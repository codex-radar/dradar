import sys,socket,importlib,importlib.abc
from pathlib import Path
def denied(*a,**k):raise AssertionError('network forbidden')
socket.socket.connect=denied;socket.create_connection=denied
source,mode,dest=sys.argv[1:];home=Path(dest)
if mode=='standalone':
 class DenyDradar(importlib.abc.MetaPathFinder):
  def find_spec(self,fullname,*args):
   if fullname=='dradar' or fullname.startswith('dradar.'):raise ModuleNotFoundError('dradar unavailable in materialized fixture')
 sys.meta_path.insert(0,DenyDradar());sys.path.insert(0,source)
 module=importlib.import_module('_dradar_pier_deepseek')
else:
 sys.path.insert(0,source)
 module=importlib.import_module('dradar.pier_deepseek')
from importlib.metadata import version
assert version('datacurve-pier')=='0.3.0'
catalog=home/'models.json';kwargs=dict(logs_dir=home/'logs',model_name='deepseek-flash',version='0.149.0',model_catalog_json_file=str(catalog),extra_env={})
a=module.DeepSeekCodex(**kwargs);assert a.network_allowlist().domains==['api.deepseek.com']
original=catalog.read_bytes();catalog.write_bytes(original+b'\n')
try:module.DeepSeekCodex(**kwargs)
except ValueError as e:assert 'integrity check failed' in str(e)
else:raise AssertionError('tamper accepted')
catalog.unlink()
try:module.DeepSeekCodex(**kwargs)
except ValueError as e:assert 'unreadable' in str(e)
else:raise AssertionError('missing accepted')
print('PASS',mode,'actualPier0.3.0 constructor/tampered/missing/networkdeny')
