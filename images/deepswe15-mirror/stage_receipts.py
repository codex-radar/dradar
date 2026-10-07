"""Stage only allowed public receipts, without scanning Docker's data root."""
import pathlib,shutil,sys
source,target=map(pathlib.Path,sys.argv[1:]);target.mkdir(parents=True,exist_ok=True)
files=['PACKAGE_PUBLIC_READBACK.json','ANONYMOUS_MANIFEST.json','ANONYMOUS_IDENTITY.json','ANONYMOUS_PULL_RECEIPT.json','FULL_ANONYMOUS_PULL.log']
for n in range(1,16):
 s=source/f'{n:03}';d=target/f'{n:03}'
 for name in files:
  p=s/name
  if p.is_file() and not p.is_symlink():d.mkdir(exist_ok=True);shutil.copyfile(p,d/name)
for name in ['BATCH_ANONYMOUS_RECEIPT.json','DAEMON_EXIT_RECEIPT.json','DOCKERD.log']:
 p=source/name
 if p.is_file() and not p.is_symlink():shutil.copyfile(p,target/name)
