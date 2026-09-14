"""Bounded, credential-free sidecar for optional managed execution evidence."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib,hmac,json,os,re,threading,uuid

MAX_BYTES=128*1024

class ObservationSink:
    def __init__(self,path,session,cohort):
        self.path=Path(path);self.session=session;self.cohort=cohort
        self.execution_id=uuid.uuid4().hex;self.sequence=0;self.dropped=0;self.lock=threading.RLock()
    def tag(self,kind,value):
        return hmac.new(self.session.local_key,('flight-v2/'+self.cohort+'/'+kind+'/'+value).encode(),hashlib.sha256).hexdigest()[:32]
    def emit(self,*args,**kwargs):
        try:return self._emit(*args,**kwargs)
        except Exception:
            with self.lock:self.dropped+=1
    def _emit(self,stage,status,*,generation=None,observed_at=None,**extra):
        with self.lock:
            self.sequence+=1
            if set(extra)-{'auth_action','auth_transaction_id','auth_events_emitted','auth_events_dropped'}:
                self.dropped+=1;return
            if stage not in {'selection','refresh','delivery','adoption','request','recovery','execution','coverage'} or status not in {'unknown','confirmed','rejected','waiting','unsupported'}:
                self.dropped+=1;return
            if ('auth_action' in extra and extra['auth_action'] not in {'start','end','ready','waiting'}) or ('auth_transaction_id' in extra and (not isinstance(extra['auth_transaction_id'],str) or not re.fullmatch('[a-f0-9]{32}',extra['auth_transaction_id']))):
                self.dropped+=1;return
            if any(type(extra[k]) is not int or not 0<=extra[k]<=2_147_483_647 for k in ('auth_events_emitted','auth_events_dropped') if k in extra):
                self.dropped+=1;return
            attrs={'provider':'codex','auth_stage':stage,'auth_status':status,'auth_delivery':'host-at',
                   'execution_id':self.execution_id,'auth_seq':self.sequence,
                   'auth_chain_tag':self.tag('chain',self.session.authority.store_id),**extra}
            try:
                revision=generation or self.session._material().revision
                if not re.fullmatch(r'[a-f0-9]{32}',revision):raise ValueError('invalid generation')
                attrs['auth_generation_tag']=self.tag('generation',revision)
                at=observed_at or datetime.now(timezone.utc).isoformat()
                if datetime.fromisoformat(at).tzinfo is None:raise ValueError('invalid observation time')
                value={'occurred_at':at,'attributes':attrs}
                raw=(json.dumps(value,separators=(',',':'))+'\n').encode()
                if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_size+len(raw)>MAX_BYTES):raise ValueError('observation capacity')
                fd=os.open(self.path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
                try:os.write(fd,raw)
                finally:os.close(fd)
            except Exception:self.dropped+=1
    def session_observation(self,attributes):
        self.emit(attributes['auth_stage'],attributes['auth_status'],**{k:v for k,v in attributes.items() if k not in {'provider','auth_stage','auth_status','auth_delivery'}})
    def coverage(self):
        self.emit('coverage','unknown',auth_events_emitted=self.sequence+1,auth_events_dropped=self.dropped)

class ObservationReader:
    def __init__(self,path,callback,assignment):
        self.path=Path(path);self.callback=callback;self.assignment=assignment;self.seen=set()
    def drain(self):
        if self.callback is None:return
        try:
            from .credential_files import read_private_credential
            raw=read_private_credential(self.path)
            if len(raw)>MAX_BYTES:return
            for line in raw.splitlines():
                try:
                    row=json.loads(line)
                    if set(row)!={'occurred_at','attributes'}:continue
                    attrs=dict(row['attributes'])
                    key=(attrs.get('execution_id'),attrs.get('auth_seq'))
                    if key in self.seen:continue
                    from .flight_recorder import _safe_attributes
                    attrs.update(owner_epoch=int(self.assignment.get('owner_epoch') or 0),attempt=int(self.assignment.get('_runner_attempt') or 1))
                    _safe_attributes(attrs)
                    at=datetime.fromisoformat(row['occurred_at'])
                    if at.tzinfo is None:continue
                    result=self.callback({**attrs,'_occurred_at':row['occurred_at']})
                    self.seen.add(key)
                except (ValueError,TypeError,KeyError):continue
        except Exception:pass
