"""Experimental review transport: bounded two-page atlases, frozen on disk."""
import hashlib
from pathlib import Path
from .pdf_page_atlas import pack_pages,verify_pages

PROFILE='two-pages-3200-edge-v1'
NOTICE=('每张图册最多包含两张完整原页，未缩放或裁剪。图册标签Source page N是pages列表的第N项，'
        '不是原PDF页码；原页码和来源摘要以pages[N-1]为准。atlas.sheets中的rect给出每页原像素区域。'
        '必须核对全部原文和全部审核目标，不得把页图当作新的公开权限。文字不可辨认时报告不确定。')


def prepare_atlas(output,stem,originals,*,frozen=None):
    output=Path(output)
    if frozen is None:
        manifest,images=pack_pages(originals,max_edge=3200,max_pages_per_sheet=2)
        frozen={'profile':PROFILE,'instructions':NOTICE,'manifest':manifest}
    else:
        if frozen.get('profile')!=PROFILE or frozen.get('instructions')!=NOTICE:
            raise ValueError('review_atlas_profile_changed')
        manifest=frozen['manifest']
        images=[(output/f'{stem}-review-atlas-{i:03d}.png').read_bytes()
                for i in range(len(manifest['sheets']))]
    if (not 1<=len(images)<=14 or any(len(sheet['pages'])>2 or max(sheet['size'])>3200
                                    for sheet in manifest['sheets'])):
        raise ValueError('review_atlas_limits_changed')
    verify_pages(originals,manifest,images)
    output.mkdir(parents=True,exist_ok=True)
    for index,raw in enumerate(images):
        path=output/f'{stem}-review-atlas-{index:03d}.png'
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sheets'][index]['sha256']:
                raise ValueError('review_atlas_bytes_changed')
        else:
            with path.open('xb') as stream:stream.write(raw)
    return frozen,images
