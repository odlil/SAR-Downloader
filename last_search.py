"""Remember one successful search location in the local user profile."""
import json
import math
from pathlib import Path
import threading

from aoi_import import geometry_bounds
from storage import atomic_json

_LOCK=threading.RLock()


def _bounds(value):
    if not isinstance(value,list) or len(value)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in value):
        return None
    w,s,e,n=value
    if not -180<=w<e<=180 or not -85<=s<n<=85:return None
    return value


def read_location(runtime):
    try:
        with (Path(runtime)/'last-search.json').open('rb') as stream:content=stream.read(4097)
        if len(content)>4096:return None
        data=json.loads(content)
        if not _bounds(data['bounds']) or not _bounds(data['aoiBounds']):return None
        stamp=data['updatedAt']
        if isinstance(stamp,bool) or not isinstance(stamp,int) or stamp<0:return None
        return {key:data[key] for key in ('bounds','aoiBounds','updatedAt')}
    except (OSError,ValueError,KeyError,TypeError):return None


def remember_location(runtime,payload,result,started_at):
    try:
        aoi=_bounds(geometry_bounds(payload['aoi'])[0])
        if not aoi:return False
        boxes=[box for scene in result['scenes'] if (box:=_bounds(scene.get('bounds'))) is not None]
        bounds=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)] if boxes else aoi
        record={'bounds':bounds,'aoiBounds':aoi,'updatedAt':int(started_at)}
        with _LOCK:
            previous=read_location(runtime)
            if previous and previous['updatedAt']>record['updatedAt']:return False
            folder=Path(runtime);folder.mkdir(parents=True,exist_ok=True)
            atomic_json(folder/'last-search.json',record)
        return True
    except (OSError,ValueError,KeyError,TypeError):return False
