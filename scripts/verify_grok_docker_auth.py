"""Opt-in inert Grok test using an INTERNAL Docker network, never live accounts.

Requires an already built production probe image plus an explicit local Node
fixture image ID. The latter only serves an inert IdP, never provider code.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dradar import grok_probe as g
from dradar import providers

IDP = r"""
const http=require('http'),fs=require('fs');
let used=false;const issuer='http://grok-idp:8123';
http.createServer((req,res)=>{
 const reply=(body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body))};
 if(req.url==='/settings')return reply({});
 if(req.url==='/models')return reply({data:[{id:'grok-4.6',model:'grok-4.6',context_window:256000}]});
 if(req.url==='/user')return reply({user_id:'inert-user'});
 if(req.url==='/.well-known/openid-configuration')return reply({issuer,authorization_endpoint:issuer+'/authorize',token_endpoint:issuer+'/token',jwks_uri:issuer+'/jwks',response_types_supported:['code'],subject_types_supported:['public'],id_token_signing_alg_values_supported:['RS256']});
 if(req.method==='POST'&&req.url==='/token'){
  let body='';req.on('data',x=>body+=x);req.on('end',()=>{
   const rt=new URLSearchParams(body).get('refresh_token');
   fs.appendFileSync('/fixture/hits.jsonl',JSON.stringify({old:rt==='INERT-OLD-RT',duplicate:used})+'\n');
   const duplicate=used;used=true;
   setTimeout(()=>reply(duplicate?{error:'invalid_grant'}:{access_token:'INERT-NEW-AT',refresh_token:'INERT-NEW-RT',token_type:'Bearer',expires_in:7200},duplicate?400:200),1500);
  });return;
 }
 reply({},404);
}).listen(8123,'0.0.0.0',()=>fs.writeFileSync('/fixture/ready','ready'));
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--fixture-image", required=True)
    args = p.parse_args()
    assert args.fixture_image.startswith("sha256:")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "idp.js").write_text(IDP)
    auth = root / "grok/auth.json"
    auth.parent.mkdir(mode=0o700)
    auth.write_text(
        json.dumps(
            {
                "http://grok-idp:8123::inert-client": {
                    "key": "INERT-OLD-AT",
                    "refresh_token": "INERT-OLD-RT",
                    "auth_mode": "oidc",
                    "user_id": "inert-user",
                    "email": None,
                    "create_time": "2026-01-01T00:00:00Z",
                    "expires_at": "2026-01-01T01:00:00Z",
                    "oidc_issuer": "http://grok-idp:8123",
                    "oidc_client_id": "inert-client",
                }
            }
        )
    )
    auth.chmod(0o600)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "HOME",
            "PATH",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "SYSTEMROOT",
            "SystemRoot",
        }
    }
    command, arch = g.local_daemon(env)
    spec_image = g._prepare_image(command, arch, auth, env)
    suffix = uuid.uuid4().hex[:12]
    network = "dradar-0113-" + suffix
    idp = "dradar-0113-idp-" + suffix
    subprocess.run(
        command + ["network", "create", "--internal", network],
        env=env,
        check=True,
        capture_output=True,
    )
    original = g._run

    def isolated(argv, **kwargs):
        if "run" in argv and "--entrypoint" not in argv:
            index = argv.index("run") + 1
            argv = (
                argv[:index]
                + [
                    "--network",
                    network,
                    "--env",
                    "GROK_OAUTH2_ISSUER=http://grok-idp:8123",
                    "--env",
                    "GROK_OAUTH2_CLIENT_ID=inert-client",
                    "--env",
                    "GROK_CLI_CHAT_PROXY_BASE_URL=http://grok-idp:8123",
                    "--env",
                    "NO_PROXY=*",
                ]
                + argv[index:]
            )
        return original(argv, **kwargs)

    try:
        subprocess.run(
            command
            + [
                "run",
                "--detach",
                "--rm",
                "--network",
                network,
                "--network-alias",
                "grok-idp",
                "--name",
                idp,
                "--mount",
                f"type=bind,source={root},target=/fixture",
                "--entrypoint",
                "node",
                args.fixture_image,
                "/fixture/idp.js",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        deadline = time.monotonic() + 10
        while not (root / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        g._run = isolated

        def runtime(index):
            # Same canonical parent as production run_probe; actual model-adapter
            # HOME and path mapping. models only: never sends a prompt.
            home = "/tmp/dradar-grok-user"
            cmd = command + [
                "run",
                "--rm",
                "--init",
                "--name",
                f"dradar-0113-runtime-{suffix}-{index}",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,mode=1777",
                "--mount",
                f"type=bind,source={auth.parent},target={home}/.grok",
                "--env",
                f"HOME={home}",
                "--env",
                f"GROK_AUTH_PATH={home}/.grok/auth.json",
                "--env",
                "GROK_TELEMETRY_ENABLED=0",
                spec_image,
                "/bin/sh",
                "-c",
                "timeout --kill-after=5s 40s /opt/grok models",
            ]
            return isolated(cmd, env=env, timeout=55)

        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = [
                pool.submit(g.run_probe, auth, root / f"probe-{i}", env)
                if i < 4
                else pool.submit(runtime, i)
                for i in range(8)
            ]
            completed = [f.result(timeout=60) for f in pending]
        hits = [
            json.loads(line) for line in (root / "hits.jsonl").read_text().splitlines()
        ]
        assert hits == [{"old": True, "duplicate": False}], hits
        after = next(iter(json.loads(auth.read_text()).values()))
        assert after["refresh_token"] == "INERT-NEW-RT"
        assert after["user_id"] == "inert-user"
        assert auth.with_name("auth.json.lock").is_file()
        assert all(result.returncode == 0 for result in completed)
        previous_env = providers.provider_subprocess_env
        providers.provider_subprocess_env = lambda: env
        try:
            issue = providers.grok_live_error("/unused-host-login-cli", auth)
            assert issue is None, issue
        finally:
            providers.provider_subprocess_env = previous_env
        # Hold the exact native lock in another Linux container. A timed-out
        # host probe must remove only itself without spending the fake RT.
        stale = json.loads(auth.read_text())
        next(iter(stale.values())).update(
            key="INERT-OLD-AT",
            refresh_token="INERT-OLD-RT",
            create_time="2026-01-01T00:00:00Z",
            expires_at="2026-01-01T01:00:00Z",
        )
        auth.write_text(json.dumps(stale))
        auth.chmod(0o600)
        holder = "dradar-0113-holder-" + suffix
        lock_inode = auth.with_name("auth.json.lock").stat().st_ino
        subprocess.run(
            command
            + [
                "run",
                "--detach",
                "--rm",
                "--network",
                "none",
                "--name",
                holder,
                "--mount",
                f"type=bind,source={auth.parent},target=/fixture",
                spec_image,
                "/bin/sh",
                "-c",
                'flock -x /fixture/auth.json.lock -c "touch /fixture/held; sleep 90"',
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (auth.parent / "held").exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)

            def short_timeout(argv, **kwargs):
                if "run" in argv and "--entrypoint" not in argv:
                    kwargs["timeout"] = 1
                return isolated(argv, **kwargs)

            g._run = short_timeout
            try:
                g.run_probe(auth, root / "timed-out", env)
                raise AssertionError("native waiter unexpectedly completed")
            except subprocess.TimeoutExpired:
                pass
            # Kill a disposable host process after its native container starts.
            # Shorten only the test's self-timeout, leaving the same production
            # timeout/kill-after/--init/--rm mechanism under test.
            child_config = {
                "command": command,
                "auth": str(auth),
                "root": str(root),
                "env": env,
                "network": network,
            }
            (root / "child-config.json").write_text(json.dumps(child_config))
            child_source = r"""
import json,sys
from pathlib import Path
from dradar import grok_probe as g
c=json.loads(Path(sys.argv[1]).read_text());original=g._run;script=g._script
g._script=lambda arch:script(arch).replace(' 40s ', ' 2s ')
def isolated(argv,**kwargs):
 if 'run' in argv and '--entrypoint' not in argv:
  name=argv[argv.index('--name')+1];Path(c['root'],'child-name').write_text(name)
  index=argv.index('run')+1
  argv=argv[:index]+['--network',c['network'],'--env','GROK_OAUTH2_ISSUER=http://grok-idp:8123','--env','GROK_OAUTH2_CLIENT_ID=inert-client','--env','GROK_CLI_CHAT_PROXY_BASE_URL=http://grok-idp:8123','--env','NO_PROXY=*']+argv[index:]
 return original(argv,**kwargs)
g._run=isolated
g.run_probe(Path(c['auth']),Path(c['root'])/'child',c['env'])
"""
            (root / "child.py").write_text(child_source)
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(root / "child.py"),
                    str(root / "child-config.json"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while not (root / "child-name").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                child_name = (root / "child-name").read_text()
                while True:
                    state = original(
                        command
                        + [
                            "container",
                            "inspect",
                            child_name,
                            "--format",
                            "{{.State.Running}}",
                        ],
                        env=env,
                    )
                    if state.returncode == 0 and state.stdout.strip() == "true":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                child.kill()
                child.wait(timeout=5)
                deadline = time.monotonic() + 12
                while (
                    original(
                        command + ["container", "inspect", child_name], env=env
                    ).returncode
                    == 0
                ):
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                if (root / "child-name").exists():
                    original(
                        command + ["rm", "--force", (root / "child-name").read_text()],
                        env=env,
                    )
            assert len((root / "hits.jsonl").read_text().splitlines()) == 1
            assert auth.with_name("auth.json.lock").stat().st_ino == lock_inode
        finally:
            g._run = isolated
            subprocess.run(
                command + ["rm", "--force", holder],
                env=env,
                capture_output=True,
                check=False,
            )
        report = {
            "real_timeout_cleanup": True,
            "host_crash_self_timeout": True,
            "lock_inode_preserved": True,
            "readiness_after_rotation": True,
            "passed": True,
            "host": platform.system(),
            "daemon_arch": arch,
            "probe_image": spec_image,
            "native_version": g.VERSION,
            "probe_containers": 4,
            "runtime_containers": 4,
            "token_posts": len(hits),
            "duplicate_refreshes": 0,
            "identity_preserved": True,
            "network": "Docker internal; no external route",
            "real_credentials": False,
            "real_models": False,
        }
        (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        g._run = original
        subprocess.run(
            command + ["rm", "--force", idp], env=env, capture_output=True, check=False
        )
        for i in range(4, 8):
            subprocess.run(
                command + ["rm", "--force", f"dradar-0113-runtime-{suffix}-{i}"],
                env=env,
                capture_output=True,
                check=False,
            )
        subprocess.run(
            command + ["network", "rm", network],
            env=env,
            capture_output=True,
            check=False,
        )


if __name__ == "__main__":
    main()
