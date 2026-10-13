from ..v2.client import Client as V2Client, ProtocolError, RemoteError
from ..v2.protocol import envelope
from . import PROTOCOL, PREFIX

class Client(V2Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.journal.bind('protocol', PROTOCOL)
        self.http.headers['X-DRadar-Protocol'] = PROTOCOL
        self.http.headers['X-DRadar-Capabilities'] = PROTOCOL + ',codex-gpt6-1-sol-v1,single-task-mixed-pool-v1'

    def bootstrap(self):
        value = envelope(self.get(PREFIX + '/bootstrap'))
        contract = value.get('execution_contract', {})
        if (PROTOCOL not in value.get('capabilities', [])
                or contract.get('max_assignments_per_run') != 1
                or contract.get('account_machine_binding') is not False
                or contract.get('auto_refill') is not False):
            raise ProtocolError('Server does not support the single-task candidate contract; upgrade required')
        account = value.get('account', {}).get('account_id')
        if not isinstance(account, str) or not account:
            raise ProtocolError('authenticated site account identity missing')
        self.journal.bind('account', account)
        self._bootstrap = value
        return value

    def _response(self, response):
        try:
            return super()._response(response)
        except RemoteError as exc:
            try:
                code=response.json().get('error',{}).get('code')
            except ValueError:
                code=None
            if isinstance(code,str):
                from ..scrub import scrub_text
                exc.code=scrub_text(code)
                exc.args=(str(exc)+'; '+exc.code+'; retain original STATE and account limits',)
            raise
