from pathlib import Path
import importlib.util, os, subprocess, sys, tempfile, zipfile
root=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('publisher',root/'scripts/ota_release.py')
tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
base='f2c199c9b6d3e2c4d7c1430327d126f097b8374a'
tree=subprocess.check_output(['git','rev-parse',base+'^{tree}'],text=True).strip()
with tempfile.TemporaryDirectory() as directory:
 package=Path(directory)/'candidate.pyz'
 tool._build_zipapp(root,package,version='0.5.201',sequence=22,commit=base,tree=tree,target=('windows' if os.name=='nt' else 'linux','x86_64'))
 for mode in ['package','standalone']:
  with tempfile.TemporaryDirectory() as d:
   home=Path(d);env=dict(os.environ,PYTHONPATH='',PYTHONDONTWRITEBYTECODE='1',DRADAR_HOME=str(home/'empty-home'))
   with zipfile.ZipFile(package) as z:(home/'models.json').write_bytes(z.read('dradar/deepseek_codex_models.json'))
   if mode=='standalone':
    code="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from dradar import runner;runner._ensure_deepseek_agent_module(Path(sys.argv[2]))"
    subprocess.run([sys.executable,'-I','-c',code,str(package),str(home)],env=env,check=True,timeout=30)
   subprocess.run([sys.executable,'-I',str(Path(__file__).with_name('probe.py')),str(home if mode=='standalone' else package),mode,str(home)],env=env,check=True,timeout=30)
