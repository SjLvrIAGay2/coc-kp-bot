"""Bounded attachment acquisition. Public HTTPS only; never trust chat paths.

Each redirect and DNS answer is checked. The production transport also resolves
and connects to a checked numeric address, preserving TLS SNI for the original
hostname, so DNS rebinding cannot turn a preflight into an internal request.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import socket
import ssl
import zipfile
import io
import uuid
from pathlib import Path
from urllib.parse import urljoin,urlsplit,unquote

import httpx
import httpcore
from httpcore._backends.anyio import AnyIOBackend

from cockp.documents.importers import MAX_IMPORT_BYTES,SUPPORTED_DOCUMENTS,checked_path


class AttachmentError(ValueError):
    """Safe code/message; never include signed URLs, credentials or remote body."""


_MIME={'.pdf':{'application/pdf'},'.docx':{'application/vnd.openxmlformats-officedocument.wordprocessingml.document','application/zip'},
    '.txt':{'text/plain'},'.md':{'text/plain','text/markdown'},'.json':{'application/json','text/plain'},
    '.png':{'image/png'},'.jpg':{'image/jpeg'},'.jpeg':{'image/jpeg'},'.webp':{'image/webp'}}
_MIME.update({'.xlsx':{'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/zip'},
    '.xls':{'application/vnd.ms-excel'},'.xlsm':{'application/vnd.ms-excel.sheet.macroenabled.12','application/zip'}})


def _public_ip(value):
    try: address=ipaddress.ip_address(value)
    except ValueError: raise AttachmentError('attachment_invalid_dns_address') from None
    mapped=getattr(address,'ipv4_mapped',None)
    if not address.is_global or address.is_reserved or address.is_multicast or address.is_unspecified or mapped is not None and not mapped.is_global:
        raise AttachmentError('attachment_private_address_blocked')
    return str(address)


async def _resolve(host,port,resolver=None):
    try: return [_public_ip(host)]
    except AttachmentError as exc:
        try: ipaddress.ip_address(host)
        except ValueError: pass
        else: raise exc
    if resolver:
        addresses=resolver(host,port)
        if inspect.isawaitable(addresses): addresses=await addresses
    else:
        answers=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
        addresses=[answer[4][0] for answer in answers]
    if not isinstance(addresses,(list,tuple)) or not addresses or len(addresses)>100: raise AttachmentError('attachment_dns_failed')
    return list(dict.fromkeys(_public_ip(value) for value in addresses))


class _PinnedBackend(AnyIOBackend):
    def __init__(self,resolver=None): self.resolver=resolver

    async def connect_tcp(self,host,port,timeout=None,local_address=None,socket_options=None):
        addresses=await _resolve(host,port,self.resolver)
        # Numeric IP pins the TCP endpoint. HTTPcore still passes the original
        # hostname to start_tls, retaining certificate/SNI validation.
        return await super().connect_tcp(addresses[0],port,timeout=timeout,
            local_address=local_address,socket_options=socket_options)


class _PublicTransport(httpx.AsyncHTTPTransport):
    def __init__(self,resolver=None):
        super().__init__(trust_env=False,retries=0)
        self._pool=httpcore.AsyncConnectionPool(ssl_context=ssl.create_default_context(),
            network_backend=_PinnedBackend(resolver),max_connections=4,max_keepalive_connections=0,retries=0)


class AttachmentFiles:
    def __init__(self,storage_root,*,max_bytes=MAX_IMPORT_BYTES,timeout=20,client=None,resolver=None):
        if type(max_bytes) is not int or not 1<=max_bytes<=50*1024*1024 or not 0<timeout<=120: raise ValueError('附件资源限制不合法')
        self.root=Path(storage_root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.max_bytes=max_bytes;self.timeout=timeout;self.client=client;self.resolver=resolver

    def local(self,path,*,is_manager,allowed_root):
        if is_manager is not True: raise AttachmentError('attachment_local_requires_manager')
        if allowed_root is None: raise AttachmentError('attachment_local_requires_allowed_root')
        try: return checked_path(path,allowed_root,self.max_bytes)
        except (ValueError,OSError): raise AttachmentError('attachment_local_path_rejected') from None

    async def _validate_url(self,url):
        if not isinstance(url,str) or not 1<=len(url)<=4096: raise AttachmentError('attachment_invalid_url')
        try:
            parsed=urlsplit(url);port=parsed.port or 443
            host=parsed.hostname
            if parsed.scheme!='https' or not host or parsed.username or parsed.password or '%' in host or '\\' in url:
                raise AttachmentError('attachment_https_required')
            if not 1<=port<=65535: raise AttachmentError('attachment_invalid_port')
            await _resolve(host,port,self.resolver)
        except AttachmentError: raise
        except (ValueError,OSError): raise AttachmentError('attachment_url_or_dns_failed') from None
        return parsed

    @staticmethod
    def _validate_type(body,suffix,mime):
        if suffix not in SUPPORTED_DOCUMENTS or mime not in _MIME[suffix]|{'application/octet-stream','binary/octet-stream',''}:
            raise AttachmentError('attachment_type_mismatch')
        valid=False
        if suffix in {'.xlsx','.xls','.xlsm'}:
            from cockp.documents.spreadsheet_import import validate_spreadsheet
            try:validate_spreadsheet(body,suffix);valid=True
            except ValueError:valid=False
        elif suffix=='.pdf': valid=body.startswith(b'%PDF-')
        elif suffix=='.docx':
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    valid={'word/document.xml','[Content_Types].xml'}<=set(archive.namelist())
            except zipfile.BadZipFile: pass
        elif suffix=='.png': valid=body.startswith(b'\x89PNG\r\n\x1a\n')
        elif suffix in {'.jpg','.jpeg'}: valid=body.startswith(b'\xff\xd8\xff')
        elif suffix=='.webp': valid=body.startswith(b'RIFF') and body[8:12]==b'WEBP'
        else:
            try:
                text=body.decode('utf-8-sig');valid='\x00' not in text and not text.lstrip().lower().startswith(('<!doctype html','<html'))
                if suffix=='.json': json.loads(text)
            except (UnicodeError,ValueError): valid=False
        if not valid: raise AttachmentError('attachment_content_signature_mismatch')

    async def download(self,url,*,filename=None):
        owned=self.client is None
        client=self.client or httpx.AsyncClient(transport=_PublicTransport(self.resolver),trust_env=False,
            timeout=self.timeout,follow_redirects=False)
        original=url
        try:
            async with asyncio.timeout(self.timeout):
                for hop in range(4):
                    parsed=await self._validate_url(url)
                    async with client.stream('GET',url,follow_redirects=False,timeout=self.timeout,
                            headers={'Accept-Encoding':'identity','Accept':'application/pdf, application/vnd.openxmlformats-officedocument.wordprocessingml.document, text/plain, image/*'}) as response:
                        if response.status_code in {301,302,303,307,308}:
                            location=response.headers.get('location')
                            if not location or hop==3: raise AttachmentError('attachment_redirect_limit')
                            url=urljoin(url,location);continue
                        if response.status_code!=200: raise AttachmentError('attachment_http_failed')
                        if response.headers.get('content-encoding','identity').lower() not in {'identity',''}: raise AttachmentError('attachment_encoded_body_rejected')
                        length=response.headers.get('content-length')
                        if length is not None:
                            try: size=int(length)
                            except ValueError: raise AttachmentError('attachment_invalid_length') from None
                            if not 0<size<=self.max_bytes: raise AttachmentError('attachment_too_large')
                        mime=response.headers.get('content-type','').split(';')[0].strip().lower()
                        label=str(filename or unquote(parsed.path).replace('\\','/').rsplit('/',1)[-1])
                        suffix=Path(label).suffix.lower()
                        if suffix not in SUPPORTED_DOCUMENTS:
                            suffix=next((extension for extension,types in _MIME.items() if mime in types),'')
                        if suffix not in SUPPORTED_DOCUMENTS: raise AttachmentError('attachment_unsupported_type')
                        body=bytearray()
                        async for part in response.aiter_bytes():
                            if len(body)+len(part)>self.max_bytes: raise AttachmentError('attachment_too_large')
                            body.extend(part)
                        if not body: raise AttachmentError('attachment_empty_body')
                        self._validate_type(body,suffix,mime)
                        digest=hashlib.sha256(body).hexdigest();path=self.root/(digest+suffix)
                        # Opaque content hashes, never user-controlled paths.
                        if not path.exists():
                            temporary=path.with_suffix(suffix+'.'+uuid.uuid4().hex+'.part')
                            try: temporary.write_bytes(body);os.replace(temporary,path)
                            finally: temporary.unlink(missing_ok=True)
                        metadata={'filename':label.replace('\\','/').rsplit('/',1)[-1][:200] or 'attachment'+suffix,
                            'sha256':digest,'bytes':len(body),'format':suffix[1:],'source_url_sha256':hashlib.sha256(original.encode()).hexdigest(),
                            'visibility':{'scope':'kp'}}
                        path.with_suffix(path.suffix+'.meta.json').write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf8')
                        return path
                raise AttachmentError('attachment_redirect_limit')
        except AttachmentError: raise
        except (httpx.HTTPError,httpcore.NetworkError,OSError,TimeoutError,ValueError):
            raise AttachmentError('attachment_transport_failed') from None
        finally:
            if owned: await client.aclose()

    async def from_group_file(self,adapter,group_id,file_id,*,busid=None,filename=None):
        if str(group_id) not in getattr(adapter,'allowed_groups',set()): raise AttachmentError('attachment_group_not_allowed')
        if not isinstance(file_id,str) or not 1<=len(file_id)<=512: raise AttachmentError('attachment_invalid_file_id')
        try: numeric_group=int(group_id)
        except (TypeError,ValueError): raise AttachmentError('attachment_invalid_group_id') from None
        payload={'group_id':numeric_group,'file_id':file_id}
        if busid is not None:
            if type(busid) is not int or busid<0: raise AttachmentError('attachment_invalid_busid')
            payload['busid']=busid
        try:
            async with asyncio.timeout(self.timeout): answer=await adapter._action('get_group_file_url',payload)
        except (TimeoutError,OSError,httpx.HTTPError): raise AttachmentError('attachment_group_extension_unavailable') from None
        if not isinstance(answer,dict) or answer.get('status')!='ok' or answer.get('retcode')!=0 or not isinstance(answer.get('data'),dict) or not isinstance(answer['data'].get('url'),str):
            raise AttachmentError('attachment_group_extension_unavailable')
        return await self.download(answer['data']['url'],filename=filename)
