"""Optional asynchronous image generation with a documented generic HTTP contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import defaultdict
from typing import Annotated, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from cockp.core.contracts import Visibility, can_view


class ImageError(RuntimeError):
    pass


class ImageCampaignConfig(BaseModel):
    """Public administrator-maintained options; global limits remain ceilings."""
    model_config = ConfigDict(extra="forbid", strict=True)
    interval_seconds: float = Field(default=30,ge=0,le=604800)
    budget: int = Field(default=10,ge=0,le=10000)
    style: str = Field(default="黑白铅笔，克制的悬疑氛围",max_length=500)
    appearance: str = Field(default="",max_length=500)
    character_appearances: dict[str,Annotated[str,Field(max_length=500)]] = Field(default_factory=dict,max_length=100)


class ImageFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=2000)
    visibility: Visibility = Field(default_factory=Visibility)
    group_id: str | None = None
    campaign_id: str | None = None


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str
    campaign_id: str
    recipient_id: str | None = None
    kind: Literal["scene", "npc", "handout", "meme"] = "scene"
    facts: list[ImageFact] = Field(max_length=30)
    # These are administrator-maintained public style/appearance descriptions.
    style: str = Field(default="黑白铅笔，克制的悬疑氛围", max_length=500)
    appearance: str = Field(default="", max_length=500)
    tense_scene: bool = False
    allow_memes: bool = False

    def visible_prompt(self) -> str:
        if self.kind == "meme" and (self.tense_scene or not self.allow_memes):
            raise ImageError("meme_blocked_by_style")
        visible = []
        for fact in self.facts:
            if fact.group_id not in (None, self.group_id) or fact.campaign_id not in (None, self.campaign_id):
                continue
            if self.recipient_id is None:
                allowed = fact.visibility.scope == "public"
            else:
                # No blanket manager flag: a private KP fact must be explicitly published/authorized first.
                allowed = can_view(fact.visibility, self.recipient_id)
            if allowed:
                visible.append(fact.text)
        if not visible:
            raise ImageError("no_visible_facts")
        return json.dumps({"kind": self.kind, "style": self.style, "appearance": self.appearance,
                           "visible_facts": visible}, ensure_ascii=False)


class ImageResult(BaseModel):
    url: str
    simulated: bool = False
    provider: str


class ImageProvider(Protocol):
    async def generate(self, prompt: str, request_id: str) -> ImageResult: ...


class DisabledProvider:
    async def generate(self, prompt: str, request_id: str) -> ImageResult:
        raise ImageError("image_disabled")


class FakeImageProvider:
    def __init__(self, *, delay: float = 0, fail: bool = False):
        self.calls: list[dict] = []
        self.delay, self.fail = delay, fail

    async def generate(self, prompt: str, request_id: str) -> ImageResult:
        self.calls.append({"request_id": request_id, "prompt": prompt})
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ImageError("fake_failure")
        return ImageResult(url="fake://" + hashlib.sha256(prompt.encode()).hexdigest()[:16],
                           simulated=True, provider="fake")


class HTTPImageProvider:
    """Our protocol, not a claimed API of an unspecified commercial provider.

    POST endpoint {request_id, prompt, count:1} -> {url:"https://..."}.
    The service MUST deduplicate Idempotency-Key across retries.
    """
    def __init__(self, endpoint: str, *, token: str = "", client: httpx.AsyncClient | None = None,
                 timeout: float = 30, allow_local_http: bool = False):
        parsed = urlparse(endpoint)
        allowed_local = allow_local_http and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if (parsed.scheme != "https" and not allowed_local) or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("image endpoint requires HTTPS or explicit local test endpoint")
        self.endpoint, self._token, self.client, self.timeout = endpoint, token, client, timeout

    async def generate(self, prompt: str, request_id: str) -> ImageResult:
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        try:
            headers = {"Idempotency-Key": request_id}
            if self._token:
                headers["Authorization"] = "Bearer " + self._token
            response = await client.post(self.endpoint, json={"request_id": request_id, "prompt": prompt, "count": 1},
                                         headers=headers, timeout=self.timeout)
            if response.status_code != 200:
                raise ImageError("image_http_" + str(response.status_code))
            url = response.json()["url"]
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ImageError("invalid_image_url")
            return ImageResult(url=url, provider="generic-http")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ImageError("image_transport_or_contract_failure") from None
        finally:
            if owned:
                await client.aclose()


class ImageJob(BaseModel):
    id: str
    group_id: str
    campaign_id: str
    recipient_id: str | None
    state: Literal["disabled", "queued", "running", "done", "failed", "cancelled", "budget", "rate_limited"]
    result: ImageResult | None = None
    cached: bool = False
    attempts: int = 0
    notice: str = ""


class ImageQueue:
    def __init__(self, provider: ImageProvider | None = None, *, global_enabled: bool = False,
                 concurrency: int = 2, max_calls_per_campaign: int = 10,
                 timeout: float = 30, retries: int = 1, min_interval: float = 30, max_queue: int = 100,
                 state: dict | None = None):
        if not 1 <= concurrency <= 10 or not 0 <= retries <= 2 or max_calls_per_campaign < 0:
            raise ValueError("invalid image budget")
        self.provider = provider or DisabledProvider()
        self.global_enabled, self.max_calls = global_enabled, max_calls_per_campaign
        self.timeout, self.retries, self.min_interval, self.max_queue = timeout, retries, min_interval, max_queue
        self.jobs: dict[str, ImageJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending: dict[str, tuple] = {}
        self._cache: dict[str, ImageResult] = {}
        self._calls: dict[tuple[str, str], int] = defaultdict(int)
        self._last_request: dict[tuple[str, str], float] = {}
        self._campaign_limits: dict[tuple[str,str],ImageCampaignConfig] = {}
        self._semaphore = asyncio.Semaphore(concurrency)
        if state:
            for entry in state.get("campaign_limits",[]):
                self._campaign_limits[(entry["group_id"],entry["campaign_id"])]=ImageCampaignConfig.model_validate(entry["config"])
            for entry in state.get("last_request",[]):
                self._last_request[(entry["group_id"],entry["campaign_id"])]=float(entry["timestamp"])
            for entry in state.get("calls", []):
                self._calls[(entry["group_id"], entry["campaign_id"])] = max(0, int(entry["count"]))+max(0,int(entry.get("reserved",0)))
            for key, value in state.get("jobs", {}).items():
                job = ImageJob.model_validate(value)
                if job.state in ("queued", "running"):
                    job.state, job.notice = "failed", "进程中断，远端结果未知；不会自动重发，请管理员核实。"
                self.jobs[key] = job

    def configure_campaign(self, group_id: str, campaign_id: str, config: ImageCampaignConfig | dict) -> dict:
        self._campaign_limits[(group_id,campaign_id)]=ImageCampaignConfig.model_validate(config)
        return self.effective_limits(group_id,campaign_id)

    def effective_limits(self, group_id: str, campaign_id: str) -> dict:
        config=self._campaign_limits.get((group_id,campaign_id))
        return {"interval_seconds":max(self.min_interval,config.interval_seconds) if config else self.min_interval,
                "budget":min(self.max_calls,config.budget) if config else self.max_calls,
                "used_calls":self._calls.get((group_id,campaign_id),0)}

    def submit(self, request: ImageRequest | dict, *, campaign_enabled: bool = False, start: bool = True) -> ImageJob:
        request = ImageRequest.model_validate(request)
        job = ImageJob(id=uuid.uuid4().hex, group_id=request.group_id, campaign_id=request.campaign_id,
                       recipient_id=request.recipient_id, state="queued")
        self.jobs[job.id] = job
        if not self.global_enabled or not campaign_enabled or isinstance(self.provider, DisabledProvider):
            job.state, job.notice = "disabled", "生图未启用，继续文字跑团。"
            return job
        try:
            prompt = request.visible_prompt()
        except ImageError:
            job.state, job.notice = "failed", "没有获准用于此图的内容，继续文字跑团。"
            return job
        key = hashlib.sha256(json.dumps([request.group_id, request.campaign_id, request.recipient_id, prompt],
                                      ensure_ascii=False).encode()).hexdigest()
        if key in self._cache:
            job.state, job.result, job.cached = "done", self._cache[key], True
            job.notice = "已复用模拟图片缓存；不是模型生成。" if job.result.simulated else "已复用获准图片缓存。"
            return job
        scope = (request.group_id, request.campaign_id)
        limits=self.effective_limits(*scope)
        if self._calls[scope] >= limits["budget"] or len(self._pending) + len([t for t in self._tasks.values() if not t.done()]) >= self.max_queue:
            job.state, job.notice = "budget", "生图预算或队列已满，继续文字跑团。"
            return job
        if time.time() - self._last_request.get(scope, -float("inf")) < limits["interval_seconds"]:
            job.state, job.notice = "rate_limited", "生图间隔未到，继续文字跑团。"
            return job
        # Wall time survives restart; a backward clock adjustment is conservative.
        self._last_request[scope] = time.time()
        self._pending[job.id] = (prompt, key, scope)
        if start:
            self.start(job.id)
        return job

    def start(self, job_id: str) -> None:
        """Activate a reserved job only after the service commits its receipt."""
        pending = self._pending.pop(job_id, None)
        if pending is not None and self.jobs[job_id].state == "queued":
            self._tasks[job_id] = asyncio.create_task(self._run(self.jobs[job_id], *pending))

    def discard(self, job_id: str) -> None:
        """Remove an uncommitted reservation; never erase a started request."""
        if job_id in self._tasks:
            raise ValueError("Cannot discard a started image request")
        self._pending.pop(job_id, None)
        self.jobs.pop(job_id, None)

    async def _run(self, job: ImageJob, prompt: str, key: str, scope: tuple[str, str]) -> None:
        try:
            async with self._semaphore:
                if not self.global_enabled:
                    job.state, job.notice = "disabled", "生图已关闭。"
                    return
                job.state = "running"
                for attempt in range(self.retries + 1):
                    if self._calls[scope] >= self.effective_limits(*scope)["budget"]:
                        job.state, job.notice = "budget", "生图调用预算用尽，继续文字跑团。"
                        return
                    self._calls[scope] += 1
                    job.attempts += 1
                    try:
                        result = await asyncio.wait_for(self.provider.generate(prompt, job.id), timeout=self.timeout)
                        self._cache[key], job.result, job.state = result, result, "done"
                        job.notice = "模拟图片；不是模型生成。" if result.simulated else "生图服务已返回图片地址。"
                        return
                    except (ImageError, TimeoutError):
                        if attempt < self.retries:
                            await asyncio.sleep(min(0.1 * 2 ** attempt, 0.5))
                job.state, job.notice = "failed", "生图超时或失败，继续文字跑团。"
        except asyncio.CancelledError:
            job.state, job.notice = "cancelled", "生图任务已取消。"

    async def wait(self, job_id: str) -> ImageJob:
        if job_id in self._tasks:
            try:
                await self._tasks[job_id]
            except asyncio.CancelledError:
                self.jobs[job_id].state = "cancelled"
        return self.jobs[job_id]

    def cancel(self, job_id: str) -> ImageJob:
        job = self.jobs[job_id]
        if job.state in ("queued", "running"):
            job.state, job.notice = "cancelled", "生图任务已取消。"
            self._pending.pop(job_id, None)
            if job_id in self._tasks:
                self._tasks[job_id].cancel()
        return job

    def disable(self, *, group_id: str | None = None, campaign_id: str | None = None) -> None:
        if group_id is None:
            self.global_enabled = False
        for job in self.jobs.values():
            if (group_id is None or job.group_id == group_id) and (campaign_id is None or job.campaign_id == campaign_id):
                self.cancel(job.id)

    def status(self) -> dict:
        return {"enabled": self.global_enabled, "jobs": {key: job.model_dump(mode="json") for key, job in self.jobs.items()},
                "campaign_limits":[{"group_id":group,"campaign_id":campaign,"config":config.model_dump(mode="json")}
                                   for (group,campaign),config in self._campaign_limits.items()],
                "last_request":[{"group_id":group,"campaign_id":campaign,"timestamp":timestamp}
                                for (group,campaign),timestamp in self._last_request.items()],
                "calls": [{"group_id": group, "campaign_id": campaign, "count": count,
                           "reserved":sum(max(0,self.retries+1-job.attempts) for job in self.jobs.values()
                                          if job.group_id==group and job.campaign_id==campaign and job.state in {"queued","running"})}
                          for (group, campaign), count in self._calls.items()]}
