"""Experimental lossless page packing, with no source or review authority."""
from __future__ import annotations

import hashlib
import io
from PIL import Image, ImageDraw

POLICY='original-pixel-page-atlas-v1'
HEADER=24
GAP=8


def _read(raw):
    with Image.open(io.BytesIO(raw)) as image:
        if image.mode not in {'RGB','L'} or max(image.size)>8192:
            raise ValueError('atlas_unsupported_image')
        return image.convert('RGB')


def pack_pages(images,*,max_edge=3200,max_images=14,max_pixels=128_000_000,max_pages_per_sheet=2):
    """Pack in original order; never resize, crop, discard, or reinterpret.

    All source pixels remain at 1:1 scale. Page labels occupy separate padding.
    The API/model may still downsample large images; live quality testing is
    required before adopting this experimental transport representation.
    """
    if (not isinstance(images,(list,tuple)) or not images or len(images)>1500
            or not 64<=max_edge<=8192 or not 1<=max_images<=14
            or type(max_pages_per_sheet) is not int or not 1<=max_pages_per_sheet<=1500
            or any(not isinstance(raw,bytes) or not raw for raw in images)
            or sum(map(len,images))>45*1024*1024):
        raise ValueError('atlas_input_limit')
    pages=[];pixel_count=0
    for index,raw in enumerate(images):
        with _read(raw) as picture:
            width,height=picture.size
            pixel_count+=width*height
            if width>max_edge or height+HEADER>max_edge or pixel_count>max_pixels:
                raise ValueError('atlas_pixel_limit')
            pages.append({'page_index':index,'source_sha256':hashlib.sha256(raw).hexdigest(),
                'size':[width,height],'rgb_sha256':hashlib.sha256(picture.tobytes()).hexdigest()})
    groups=[];group=[];x=y=row_height=0
    for page in pages:
        width,height=page['size']
        if len(group)>=max_pages_per_sheet:
            groups.append(group);group=[];x=y=row_height=0
        if x+width>max_edge:
            x=0;y+=row_height+GAP;row_height=0
        if y+HEADER+height>max_edge:
            groups.append(group);group=[];x=y=row_height=0
        group.append({**page,'rect':[x,y+HEADER,x+width,y+HEADER+height]})
        x+=width+GAP;row_height=max(row_height,height+HEADER)
    if group:groups.append(group)
    if len(groups)>max_images:
        raise ValueError('atlas_image_limit')
    outputs=[];sheets=[];canvas_pixels=0
    for index,group in enumerate(groups):
        size=(max(p['rect'][2] for p in group),max(p['rect'][3] for p in group))
        canvas_pixels+=size[0]*size[1]
        if canvas_pixels>max_pixels:
            raise ValueError('atlas_canvas_limit')
        with Image.new('RGB',size,'white') as sheet:
            draw=ImageDraw.Draw(sheet)
            for page in group:
                left,top,_,_=page['rect']
                draw.text((left+2,top-HEADER+5),f"Source page {page['page_index']+1}",fill='black')
                with _read(images[page['page_index']]) as picture:
                    sheet.paste(picture,(left,top))
            stream=io.BytesIO();sheet.save(stream,format='PNG');raw=stream.getvalue()
        outputs.append(raw)
        sheets.append({'sheet_index':index,'size':list(size),'sha256':hashlib.sha256(raw).hexdigest(),'pages':group})
    if sum(map(len,outputs))>32*1024*1024:
        raise ValueError('atlas_output_limit')
    return {'policy':POLICY,'source_count':len(images),'sheets':sheets},outputs


def verify_pages(images,manifest,atlases):
    """Verify exact original pixels and exhaustive, unique page membership."""
    if (manifest.get('policy')!=POLICY or manifest.get('source_count')!=len(images)
            or len(manifest['sheets'])!=len(atlases)):
        raise ValueError('atlas_binding_changed')
    seen=[]
    for index,(entry,raw) in enumerate(zip(manifest['sheets'],atlases)):
        if entry['sheet_index']!=index or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
            raise ValueError('atlas_bytes_changed')
        with _read(raw) as sheet:
            if list(sheet.size)!=entry['size']:raise ValueError('atlas_size_changed')
            for page in entry['pages']:
                number=page['page_index']
                if type(number) is not int or not 0<=number<len(images) or number in seen:
                    raise ValueError('atlas_page_membership_changed')
                seen.append(number)
                with _read(images[number]) as original:
                    left,top,right,bottom=page['rect']
                    if (page['source_sha256']!=hashlib.sha256(images[number]).hexdigest()
                            or page['size']!=list(original.size)
                            or not 0<=left<right<=sheet.width or not 0<=top<bottom<=sheet.height
                            or [right-left,bottom-top]!=page['size']):
                        raise ValueError('atlas_page_binding_changed')
                    with sheet.crop(page['rect']) as restored:
                        digest=hashlib.sha256(restored.tobytes()).hexdigest()
                        if digest!=page['rgb_sha256'] or restored.tobytes()!=original.tobytes():
                            raise ValueError('atlas_page_pixels_changed')
    if seen!=list(range(len(images))):
        raise ValueError('atlas_page_coverage_changed')
