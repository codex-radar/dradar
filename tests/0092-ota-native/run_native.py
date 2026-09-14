"""Collect only candidate tests plus a byte-verified isolated native supplement."""
import hashlib,json,os,subprocess,sys,tempfile
from pathlib import Path
import pytest

def main():
    root=Path(__file__).parent
    candidate=Path(sys.argv[1]).resolve()
    source=candidate/'src'
    assert (source/'dradar/ota/discovery.py').is_file()
    supplement=subprocess.check_output(['git','-C',str(root),'show','HEAD:tests/0092-ota-native/test_windows_native.py'])
    sys.path[:0]=[str(source),str(candidate/'tests')]
    os.environ['PYTHONPATH']=str(source)+os.pathsep+str(candidate/'tests')
    os.environ['DRADAR_EXPECTED_SOURCE']=str(source)
    assert Path.cwd().resolve() == candidate, 'workflow working-directory mismatch'
    probe = subprocess.check_output([
        sys.executable, '-c',
        "import json,dradar,dradar.ota.discovery;print(json.dumps([dradar.__file__,dradar.ota.discovery.__file__]))",
    ], env=os.environ.copy(), cwd=candidate, text=True)
    for location in json.loads(probe):
        assert Path(location).resolve().is_relative_to(source), location
    print('SUBPROCESS_SOURCE_GUARD '+probe.strip())

    class SourceGuard:
        def pytest_collection_finish(self,session):
            assert Path(session.config.rootpath).resolve() == candidate
            verified={}
            for name,module in list(sys.modules.items()):
                if name=='dradar' or name.startswith('dradar.'):
                    location=getattr(module,'__file__',None)
                    if location:
                        resolved=Path(location).resolve()
                        assert resolved.is_relative_to(source), (name,str(resolved))
                        verified[name]=str(resolved.relative_to(source))
            assert 'dradar.ota.discovery' in verified
            print('CANDIDATE_IMPORT_GUARD '+json.dumps(verified,sort_keys=True))

    with tempfile.TemporaryDirectory(prefix='0092-native-supplement-') as directory:
        native=Path(directory)/'test_windows_native.py'
        native.write_bytes(supplement)
        assert native.read_bytes()==supplement
        print('NATIVE_SUPPLEMENT_SHA256 '+hashlib.sha256(supplement).hexdigest())
        args=['-q','--rootdir',str(candidate)]
        if sys.argv[2:]==['--collect-only']:args+=['--collect-only']
        else:assert len(sys.argv)==2
        args += [str(candidate/'tests'/name) for name in (
            'test_ota_discovery.py','test_ota_runtime.py','test_ota_state.py',
            'test_ota_integration.py','test_ota_multiprocess.py',
        )]
        args.append(str(native))
        raise SystemExit(pytest.main(args,plugins=[SourceGuard()]))


if __name__ == "__main__":
    main()
