"""Pier adapter for official Kiro CLI ACP v3, pinned to Claude Opus 5.5.

The Linux CLI receives only an owner-private copy of one social session.
ACP config selection and exact native session evidence are checked before a
result can be submitted.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
import uuid
from datetime import datetime
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
    import _dradar_kiro_acp_runtime as kiro_acp_runtime
except ModuleNotFoundError:
    from dradar import kiro_acp_runtime
try:
    from _dradar_worker_events import register_worker, verify_task_baseline
except ModuleNotFoundError:
    from dradar.worker_events import register_worker, verify_task_baseline
try:
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.artifact_boundary import private_post_run


VERSION = "2.26.0"
KIRO_CREDIT_USD_RATE = Decimal("0.04")
KIRO_CREDIT_RATE_VERSION = "stationmaster-2026-09-28-v1"
LANE_MODEL = "kiro-claude-opus-5.5"
REQUEST_MODEL = "claude-opus-5.5"
ARCHIVE_SHA256 = {
    "x86_64": "fad32095530facd3ed28d4210798804d2340373643b69f256492798f9befd479",
    "aarch64": "bdef2a21a82d8e40d73cfae233710adc56ad9c99d4d0f8ceccbc74206912ce7b",
}


def _version_check(cli: str) -> str:
    expected = shlex.quote("kiro-cli " + VERSION)
    return f'actual=$({shlex.quote(cli)} --version) && [ "$actual" = {expected} ]'


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
        + _version_check("/opt/dradar-kiro/bin/kiro-cli")
    )


_BOOTSTRAP = r'''import json,os,sqlite3,sys
from pathlib import Path
source=Path(sys.argv[1]); home=Path(sys.argv[2]); model=sys.argv[3]; effort=sys.argv[4]
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
    'chat.modelDefaults':{model:{'effort':effort}}},separators=(',',':')))
os.chmod(settings,0o600)
source.unlink()
'''


_VERIFY = r'''import json,sys
from pathlib import Path
stream=Path(sys.argv[1]);home=Path(sys.argv[2]);expected=sys.argv[3];expected_effort=sys.argv[4]
events=[json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
finishes=[e.get('data',{}) for e in events if e.get('type')=='runFinished']
if len(finishes)!=1 or finishes[0].get('status')!='success':
    raise SystemExit('DRADAR_KIRO_ATTESTATION=run_failed')
stream_sid=finishes[0].get('sessionId')
if not isinstance(stream_sid,str) or not stream_sid:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=session_missing')
selected=[e.get('data',{}) for e in events if e.get('type')=='configSelected']
if (len(selected)!=1 or selected[0].get('sessionId')!=stream_sid
        or selected[0].get('model')!=expected
        or selected[0].get('effort')!=expected_effort):
    raise SystemExit('DRADAR_KIRO_ATTESTATION=acp_config_missing')
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
        observed_effort=data.get('effortLevel')
    else:
        sid=data['session_id']
        if sid!=sidecars[0].stem or data['cwd']!='/app':
            raise ValueError('native session identity mismatch')
        actual=data['session_state']['rts_model_state']['model_info']['model_id']
        observed_effort=None
except (OSError,KeyError,TypeError,ValueError):
    raise SystemExit('DRADAR_KIRO_ATTESTATION=model_evidence_missing')
if actual!=expected:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=model_mismatch')
if observed_effort!=expected_effort:
    raise SystemExit('DRADAR_KIRO_ATTESTATION=effort_mismatch')
attestation={'schema':'dradar-kiro-model-v1','requested_model':expected,
             'observed_model':actual,'session_id':sid,
             'stream_session_id':stream_sid,'run_status':'success',
             'requested_effort':expected_effort,'observed_effort':observed_effort}
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
    _ACP = PurePosixPath("/tmp/dradar-kiro-user/acp-runtime.py")
    _STREAM = "kiro-stream.jsonl"
    _STREAM_FILE = _STREAM

    @staticmethod
    def _return_marker(source: Path) -> Path:
        return source.with_name(source.name + ".return-pending")

    def _begin_credential_return(self) -> Path:
        # The host must retain its private snapshot if Pier exits at any point
        # after the native CLI may have refreshed the container credential.
        marker = self._return_marker(self._auth_file)
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"Kiro private credential return is pending\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        return marker

    async def _return_credential(self, environment: BaseEnvironment,
                                 env: dict[str, str], home: str, auth: str,
                                 marker: Path) -> None:
        db = home + "/.local/share/kiro-cli/data.sqlite3"
        export = "python3 -c " + shlex.quote(_EXPORT) + " " + " ".join(map(shlex.quote,
            (db, auth)))
        result = await self.exec_as_agent(environment, command=export, env=env)
        if result.return_code != 0:
            raise RuntimeError("Kiro private credential export failed; host snapshot retained")
        fd, name = tempfile.mkstemp(prefix=self._auth_file.name + ".returned-",
                                    dir=self._auth_file.parent)
        os.close(fd)
        staged = Path(name)
        try:
            await environment.download_file(auth, staged)
            info = staged.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("Kiro returned credential is not a regular file")
            if os.name != "nt":
                os.chmod(staged, 0o600)
            original = json.loads(self._auth_file.read_text(encoding="utf-8"))
            returned = json.loads(staged.read_text(encoding="utf-8"))
            required = ("access_token", "refresh_token", "expires_at", "provider",
                        "profile_arn")
            if (not isinstance(original, dict) or not isinstance(returned, dict)
                    or any(not isinstance(returned.get(key), str) or not returned[key]
                           for key in required)
                    or any(returned[key] != original.get(key)
                           for key in ("provider", "profile_arn"))):
                raise RuntimeError("Kiro returned credential failed identity validation")
            original_expiry = datetime.fromisoformat(original["expires_at"].replace("Z", "+00:00"))
            returned_expiry = datetime.fromisoformat(returned["expires_at"].replace("Z", "+00:00"))
            if (original_expiry.tzinfo is None or returned_expiry.tzinfo is None
                    or (returned != original and returned_expiry <= original_expiry)):
                raise RuntimeError("Kiro returned credential failed expiry validation")
            os.replace(staged, self._auth_file)
            marker.unlink()
        except Exception as exc:
            # Keep any returned bytes owner-only for manual recovery. The host
            # context sees the marker and refuses to treat the run as complete.
            if staged.exists() and os.name != "nt":
                os.chmod(staged, 0o600)
            raise RuntimeError("Kiro private credential return failed; host snapshot retained") from exc

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
            verification_command=_version_check(self._CLI),
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
        await environment.upload_file(Path(kiro_acp_runtime.__file__), str(self._ACP))
        # Let the official CLI create its own migration schema before adding
        # the private social token. Doctor may report no login at this point.
        await self.exec_as_agent(environment,command=(
            f"{shlex.quote(cli)} doctor >/dev/null 2>&1 || true"
        ),env=env)
        await inject_private_files(self,environment,[(self._auth_file,auth)])
        bootstrap=("python3 -c "+shlex.quote(_BOOTSTRAP)+" "+" ".join(map(shlex.quote,
            (auth,home,REQUEST_MODEL,self._effort))))
        await self.exec_as_agent(environment,command=bootstrap,env=env)
        # A task may ship its own Kiro workspace settings. Never allow them to
        # override the private HOME model pin without explicit verification.
        await self.exec_as_agent(environment,command=(
            "test ! -e /app/.kiro/settings/cli.json"
        ),env=env)
        marker=self._begin_credential_return()
        stream="/logs/agent/"+self._STREAM
        command=("python3 "+shlex.quote(str(self._ACP))+" "+" ".join(map(shlex.quote,
                 (cli,stream,REQUEST_MODEL,self._effort,instruction)))+
                 " 2> /logs/agent/kiro-stderr.log")
        try:
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
            completed=await self.exec_as_agent(environment,command=command,env=env,cwd="/app")
            if completed.return_code != 0:
                raise RuntimeError("Kiro ACP runner failed")
            verify="python3 -c "+shlex.quote(_VERIFY)+" "+" ".join(map(shlex.quote,
                (stream,home,REQUEST_MODEL,self._effort)))
            verified=await self.exec_as_agent(environment,command=verify,env=env)
            if verified.return_code != 0:
                raise RuntimeError("Kiro native model or effort attestation failed")
        finally:
            await self._return_credential(environment, env, home, auth, marker)

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
        if (evidence.get("requested_effort")!=self._effort
                or evidence.get("observed_effort")!=self._effort):
            return
        steps=[]
        text_parts=[]
        tool_kinds={}
        def flush_text() -> None:
            if text_parts:
                steps.append(Step(step_id=len(steps)+1,source="agent",
                    message="".join(text_parts),model_name=LANE_MODEL,
                    reasoning_effort=self._effort,llm_call_count=None))
                text_parts.clear()
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
                elif update.get("sessionUpdate") in ("tool_call","tool_call_update"):
                    flush_text()
                    tool_id=update.get("toolCallId")
                    status=update.get("status")
                    kind=update.get("kind")
                    if isinstance(tool_id,str) and isinstance(kind,str):
                        tool_kinds[tool_id]=kind
                    elif isinstance(tool_id,str):
                        kind=tool_kinds.get(tool_id)
                    steps.append(Step(step_id=len(steps)+1,source="agent",
                        message="Kiro ACP tool " + str(kind or "unknown") + " " + str(status or "unknown"),
                        model_name=LANE_MODEL,reasoning_effort=self._effort,
                        llm_call_count=None,extra={"acp_tool_call_id":tool_id,
                            "acp_update":update["sessionUpdate"]}))
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
        flush_text()
        if not steps:
            steps=[Step(step_id=1,source="agent",message="Kiro run completed",
                        model_name=LANE_MODEL,reasoning_effort=self._effort,llm_call_count=None)]
        metrics=FinalMetrics(total_prompt_tokens=None,total_completion_tokens=None,
            total_cached_tokens=None,total_cost_usd=None,total_steps=len(steps),
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
            "thinking_effort_verified":True,
            "verified_thinking_effort":self._effort,
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
