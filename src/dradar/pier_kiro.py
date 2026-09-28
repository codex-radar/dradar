"""Pier adapter for the official Kiro CLI, pinned to Claude Opus 5.5.

The Linux CLI receives only an owner-private copy of one social session. Its
global model preference is set inside a disposable HOME. Kiro 2.24.1 can
ignore ``--model`` in headless mode, so the persisted session model is checked
after every run before any result can be submitted.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.trajectories import Agent, FinalMetrics, Step, Trajectory
from pier.utils.trajectory_metrics import populate_context_from_final_metrics

try:
    from _dradar_pier_credential_delivery import inject_private_files
except ModuleNotFoundError:
    from dradar.pier_credential_delivery import inject_private_files
try:
    from _dradar_worker_events import register_worker, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import register_worker, verify_task_baseline
try:
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.artifact_boundary import private_post_run


VERSION = "2.24.1"
KIRO_CREDIT_USD_RATE = Decimal("0.04")
KIRO_CREDIT_RATE_VERSION = "stationmaster-2026-09-28-v1"
LANE_MODEL = "kiro-claude-opus-5.5"
REQUEST_MODEL = "claude-opus-5.5"
ARCHIVE_SHA256 = {
    "x86_64": "89e26b61707a3a17bcf0d0dfa7043366d94b48bfe35e9605058bd5de4b7199ce",
    "aarch64": "fab94745fb92c3d5d40c39432757edc4740fe40ed3d681a61129e790c2fe2f10",
}


def _install_command() -> str:
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        " echo 'Kiro requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        " apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        " --no-install-recommends ca-certificates curl unzip python3; "
        "elif command -v dnf >/dev/null 2>&1; then "
        " dnf install -y ca-certificates curl unzip python3; "
        "elif command -v yum >/dev/null 2>&1; then "
        " yum install -y ca-certificates curl unzip python3; "
        "else echo 'No supported package manager' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f" x86_64) arch=x86_64; sha={ARCHIVE_SHA256['x86_64']} ;; "
        f" aarch64|arm64) arch=aarch64; sha={ARCHIVE_SHA256['aarch64']} ;; "
        " *) echo 'Unsupported architecture' >&2; exit 1 ;; esac; "
        'tmp=$(mktemp -d); trap \'rm -rf -- "${tmp}"\' EXIT; '
        f'url="https://prod.download.cli.kiro.dev/stable/{VERSION}/kirocli-${{arch}}-linux.zip"; '
        'curl --fail --silent --show-error --location --connect-timeout 15 '
        ' --max-time 240 --output "${tmp}/kiro.zip" "${url}"; '
        'printf "%s  %s\\n" "${sha}" "${tmp}/kiro.zip" | sha256sum --check --strict -; '
        'unzip -q "${tmp}/kiro.zip" -d "${tmp}/unpacked"; '
        'install -d -m 0755 /opt/dradar-kiro/bin; '
        'cp -a "${tmp}/unpacked/kirocli/bin/." /opt/dradar-kiro/bin/; '
        'chmod 0755 /opt/dradar-kiro/bin/*; '
        f'/opt/dradar-kiro/bin/kiro-cli --version | grep -Fq {shlex.quote(VERSION)}'
    )


_BOOTSTRAP = r'''import json,os,sqlite3,sys
from pathlib import Path
source=Path(sys.argv[1]); home=Path(sys.argv[2]); model=sys.argv[3]
token=json.loads(source.read_text())
assert set(('access_token','refresh_token','expires_at','provider','profile_arn')) <= token.keys()
assert token['provider'] in ('google','github')
assert token['profile_arn'].startswith('arn:aws:codewhisperer:')
db=home/'.local/share/kiro-cli/data.sqlite3';db.parent.mkdir(parents=True,exist_ok=True)
os.chmod(db.parent,0o700)
c=sqlite3.connect(db)
tables={row[0] for row in c.execute("select name from sqlite_master where type='table'")}
if not {'auth_kv','migrations','state'} <= tables:
    raise SystemExit('Kiro native credential database was not initialized')
c.execute('insert or replace into auth_kv(key,value) values(?,?)',
          ('kirocli:social:token',json.dumps(token,separators=(',',':'))))
c.commit();c.close();os.chmod(db,0o600)
settings=home/'.kiro/settings/cli.json';settings.parent.mkdir(parents=True,exist_ok=True)
os.chmod(settings.parent,0o700)
settings.write_text(json.dumps({'chat.defaultModel':model,
    'chat.modelDefaults':{model:{'effort':'high'}}},separators=(',',':')))
os.chmod(settings,0o600)
source.unlink()
'''


_VERIFY = r'''import json,sys
from pathlib import Path
stream=Path(sys.argv[1]);home=Path(sys.argv[2]);expected=sys.argv[3]
events=[json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
finishes=[e.get('data',{}) for e in events if e.get('type')=='runFinished']
if len(finishes)!=1 or finishes[0].get('status')!='success':
    raise SystemExit('DRADAR_KIRO_ATTESTATION=run_failed')
stream_sid=finishes[0].get('sessionId')
if not isinstance(stream_sid,str) or not stream_sid:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=session_missing')
legacy=list((home/'.kiro/sessions/cli').glob('*.json'))
native=list((home/'.kiro/sessions').glob('*/sess_*/session.json'))
sidecars=legacy+native
if len(sidecars)!=1:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=native_session_ambiguous')
try:
    data=json.loads(sidecars[0].read_text())
    if sidecars[0].name=='session.json':
        sid=data['id']
        if (sid!=sidecars[0].parent.name or sid!=stream_sid
                or '/app' not in data['workspacePaths']
                or '/app' not in data['rootPaths']):
            raise ValueError('native session identity mismatch')
        actual=data['modelId']
    else:
        sid=data['session_id']
        if sid!=sidecars[0].stem or data['cwd']!='/app':
            raise ValueError('native session identity mismatch')
        actual=data['session_state']['rts_model_state']['model_info']['model_id']
except (OSError,KeyError,TypeError,ValueError):
    raise SystemExit('DRADAR_KIRO_ATTESTATION=model_evidence_missing')
if actual!=expected:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=model_mismatch')
attestation={'schema':'dradar-kiro-model-v1','requested_model':expected,
             'observed_model':actual,'session_id':sid,
             'stream_session_id':stream_sid,'run_status':'success'}
Path('/logs/agent/kiro-attestation.json').write_text(json.dumps(attestation))
metered=[]
if sidecars[0].name=='session.json':
    message_path=sidecars[0].with_name('messages.jsonl')
    try:
        messages=[json.loads(line) for line in message_path.read_text().splitlines()
                  if line.strip()]
        summaries=[row['payload']['promptTurnSummaries'] for row in messages
                   if isinstance(row,dict)
                   and isinstance(row.get('payload'),dict)
                   and row['payload'].get('type')=='usage_summary']
        if (len(summaries)==1 and isinstance(summaries[0],list)
                and summaries[0] and all(isinstance(item,dict)
                    and 'usage' in item and 'unit' in item for item in summaries[0])):
            metered=[{'value':item['usage'],'unit':item['unit']}
                     for item in summaries[0]]
    except (OSError,KeyError,TypeError,ValueError):
        metered=[]
else:
    turns=data['session_state'].get('conversation_metadata',{}).get('user_turn_metadatas')
    if isinstance(turns,list) and turns:
        for turn in turns:
            if not isinstance(turn,dict) or turn.get('model')!=expected:
                metered=[];break
            usage=turn.get('metering_usage')
            if not isinstance(usage,list) or not usage:
                metered=[];break
            metered.extend(usage)
Path('/logs/agent/kiro-metering.json').write_text(json.dumps({
    'schema':'dradar-kiro-native-metering-v1','session_id':sid,
    'metering_usage':metered}))
'''


_EXPORT = r'''import json,sqlite3,sys
from pathlib import Path
db=Path(sys.argv[1]);dest=Path(sys.argv[2]);c=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
row=c.execute("select value from auth_kv where key='kirocli:social:token'").fetchone();c.close()
if row is None:raise SystemExit(1)
token=json.loads(row[0]);dest.write_text(json.dumps(token,separators=(',',':')))
dest.chmod(0o600)
'''


class KiroOpus55(BaseInstalledAgent):
    SUPPORTS_ATIF = True
    _HOME = PurePosixPath("/tmp/dradar-kiro-user")
    _AUTH = PurePosixPath("/tmp/dradar-kiro-auth/token.json")
    _CLI = PurePosixPath("/opt/dradar-kiro/bin/kiro-cli")
    _STREAM = "kiro-stream.jsonl"
    _STREAM_FILE = _STREAM

    @staticmethod
    def name() -> str:
        return "kiro"

    def __init__(self, *args: Any, auth_json_file: str, reasoning_effort: str,
                 **kwargs: Any):
        self._auth_file = Path(auth_json_file)
        if not self._auth_file.is_file():
            raise ValueError("Kiro CLI social run credential is missing")
        if reasoning_effort != "high":
            raise ValueError("Kiro Opus 5.5 supports only the high lane")
        self._effort = reasoning_effort
        super().__init__(*args, **kwargs)
        if self.model_name != LANE_MODEL:
            raise ValueError("Kiro adapter requires its isolated Opus 5.5 lane")

    def get_version_command(self) -> str:
        return f"{self._CLI} --version"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(), version=VERSION,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=f"{self._CLI} --version | grep -Fq {shlex.quote(VERSION)}",
            cache_key=f"dradar-kiro-{VERSION}-official-zip-v1",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=[
            "prod.us-east-1.auth.desktop.kiro.dev",
            "codewhisperer.us-east-1.amazonaws.com",
            "management.us-east-1.kiro.dev",
            "runtime.us-east-1.kiro.dev",
        ])

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        await verify_task_baseline(environment)
        await register_worker(runtime="pier", context="agent", profile="kiro")
        del context
        home=str(self._HOME);auth=str(self._AUTH);cli=str(self._CLI)
        env=self.build_process_env({"HOME":home,"BROWSER":"/usr/bin/false"})
        env.update({"HOME":home,"BROWSER":"/usr/bin/false",
                    "PATH":"/opt/dradar-kiro/bin:"+env.get("PATH","/usr/bin:/bin")})
        for key in ("AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","AWS_SESSION_TOKEN",
                    "AWS_PROFILE","ANTHROPIC_API_KEY","OPENAI_API_KEY"):
            env.pop(key,None)
        await self.exec_as_agent(environment,command=(
            f"mkdir -p {shlex.quote(home)} && chmod 700 {shlex.quote(home)}"
        ),env=env)
        # Let the official CLI create its own migration schema before adding
        # the private social token. Doctor may report no login at this point.
        await self.exec_as_agent(environment,command=(
            f"{shlex.quote(cli)} doctor >/dev/null 2>&1 || true"
        ),env=env)
        await inject_private_files(self,environment,[(self._auth_file,auth)])
        bootstrap=("python3 -c "+shlex.quote(_BOOTSTRAP)+" "+" ".join(map(shlex.quote,
            (auth,home,REQUEST_MODEL))))
        await self.exec_as_agent(environment,command=bootstrap,env=env)
        # A task may ship its own Kiro workspace settings. Never allow them to
        # override the private HOME model pin without explicit verification.
        await self.exec_as_agent(environment,command=(
            "test ! -e /app/.kiro/settings/cli.json"
        ),env=env)
        catalog=await self.exec_as_agent(environment,command=(
            f"{shlex.quote(cli)} chat --list-models --format json "
            "| python3 -c "+shlex.quote(
                "import json,sys; x=json.load(sys.stdin); "
                "assert any(m.get('model_id')=='claude-opus-5.5' "
                "for m in x.get('models',[]))"
            )
        ),env=env)
        if catalog.return_code != 0:
            raise RuntimeError("Kiro Opus 5.5 catalog preflight failed")
        stream="/logs/agent/"+self._STREAM
        command=(f"{shlex.quote(cli)} chat --v3 --no-interactive --trust-all-tools "
                 f"--effort high --output-format stream-json {shlex.quote(instruction)} "
                 f"> {shlex.quote(stream)} 2> /logs/agent/kiro-stderr.log")
        try:
            await self.exec_as_agent(environment,command=command,env=env,cwd="/app")
            verify="python3 -c "+shlex.quote(_VERIFY)+" "+" ".join(map(shlex.quote,
                (stream,home,REQUEST_MODEL)))
            await self.exec_as_agent(environment,command=verify,env=env)
        finally:
            db=home+"/.local/share/kiro-cli/data.sqlite3"
            export="python3 -c "+shlex.quote(_EXPORT)+" "+" ".join(map(shlex.quote,
                (db,auth)))
            try:
                await self.exec_as_agent(environment,command=export,env=env)
                await environment.download_file(auth,self._auth_file)
                if os.name!="nt":os.chmod(self._auth_file,0o600)
            except Exception:
                self.logger.warning("Kiro refreshed credential could not be recovered")

    @private_post_run
    def populate_context_post_run(self, context: AgentContext) -> None:
        try:
            events=[json.loads(line) for line in (self.logs_dir/self._STREAM).read_text(
                encoding="utf-8",errors="replace").splitlines() if line.strip()]
            evidence=json.loads((self.logs_dir/"kiro-attestation.json").read_text())
            metering=json.loads((self.logs_dir/"kiro-metering.json").read_text())
        except (OSError,ValueError):
            return
        if evidence.get("observed_model")!=REQUEST_MODEL:
            return
        text_parts=[]
        credit_values=[]
        credit_valid=(metering.get("schema")=="dradar-kiro-native-metering-v1"
                      and metering.get("session_id")==evidence.get("session_id"))
        for event in events:
            if event.get("type")=="sessionUpdate":
                update=event.get("data",{}).get("update",{})
                if update.get("sessionUpdate")=="agent_message_chunk":
                    content=update.get("content",{})
                    if isinstance(content,dict) and isinstance(content.get("text"),str):
                        text_parts.append(content["text"])
        usage=metering.get("metering_usage")
        if not isinstance(usage,list) or not usage:
            credit_valid=False
        else:
            for item in usage:
                try:
                    if (not isinstance(item,dict)
                            or item.get("unit") not in ("credit","credits")
                            or isinstance(item.get("value"),bool)):
                        raise ValueError("unsupported Kiro meter")
                    amount=Decimal(str(item["value"]))
                    if not amount.is_finite() or amount < 0:
                        raise ValueError("invalid Kiro credit amount")
                    credit_values.append(amount)
                except (KeyError,TypeError,ValueError,InvalidOperation):
                    credit_valid=False
        credits=sum(credit_values,Decimal(0)) if credit_valid and credit_values else None
        estimated_usd=(credits*KIRO_CREDIT_USD_RATE if credits is not None else None)
        steps=[Step(step_id=1,source="agent",message="".join(text_parts) or "Kiro run completed",
                    model_name=LANE_MODEL,reasoning_effort="high",llm_call_count=None)]
        metrics=FinalMetrics(total_prompt_tokens=None,total_completion_tokens=None,
            total_cached_tokens=None,total_cost_usd=None,total_steps=1,
            extra={"billing_basis":"subscription","cost_not_reported":True})
        trajectory=Trajectory(schema_version="ATIF-v1.7",
            session_id=evidence.get("session_id") or str(uuid.uuid4()),
            agent=Agent(name=self.name(),version=VERSION,model_name=LANE_MODEL,
                        extra={"provider":"kiro-subscription","observed_model":REQUEST_MODEL}),
            steps=steps,final_metrics=metrics)
        (self.logs_dir/"trajectory.json").write_text(json.dumps(trajectory.to_json_dict(),
            ensure_ascii=False,indent=2),encoding="utf-8")
        (self.logs_dir/"provider-usage.json").write_text(json.dumps({
            "schema":"dradar-subscription-provider-usage-v1",
            "provider":"kiro","model":LANE_MODEL,"observed_model":REQUEST_MODEL,
            "observed_model_status":"session-metadata-verified",
            "kiro_credits":float(credits) if credits is not None else None,
            "kiro_estimated_usd":float(estimated_usd) if estimated_usd is not None else None,
            "kiro_credit_rate_usd":float(KIRO_CREDIT_USD_RATE),
            "kiro_credit_rate_version":KIRO_CREDIT_RATE_VERSION,
            "kiro_credit_source":"official-kiro-cli-per-turn-metering",
            "complete":False,"request_count":None,
            "n_input_tokens":None,"n_cache_tokens":None,"n_output_tokens":None,
            "request_usage_observed":False,
            "request_usage_complete":False,"timed_usage_complete":False,
            "usage_incomplete_reason":"kiro_cli_does_not_expose_token_ledger",
            "usage_evidence_tier":"unavailable",
        },ensure_ascii=False),encoding="utf-8")
        populate_context_from_final_metrics(context,metrics)


__all__=["KiroOpus55"]
