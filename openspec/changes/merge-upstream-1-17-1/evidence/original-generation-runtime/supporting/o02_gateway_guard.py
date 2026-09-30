"""Acceptance-only transparent bounded Agent LLM gateway. No provider keys or model calls."""
import hashlib
import http.client
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LLM_PATH = '/inner/api/agent/llm/invoke'
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length'}
CALLER_REQUIRED = {'invocation_id', 'agent_run_id', 'call_index', 'tenant_id', 'user_id',
                   'user_from', 'app_id', 'invoke_from', 'agent_mode'}
CALLER_OPTIONAL = {'conversation_id', 'workflow_id', 'workflow_run_id', 'node_id',
                   'node_execution_id', 'agent_id', 'agent_config_version_id',
                   'agent_config_version_kind', 'trace_id'}
TARGET_KEYS = {'provider', 'model', 'prompt_messages', 'model_parameters', 'tools', 'stop', 'stream'}

class GuardState:
    def __init__(self, policy, key, ledger_path, upstream):
        if not key or policy['forward_limit'] != 2 or policy['max_output_tokens'] != 8:
            raise ValueError('GuardConfigurationInvalid')
        self.policy, self.key = policy, key
        self.path = Path(ledger_path)
        self.upstream = urlsplit(upstream)
        if self.upstream.scheme not in {'http', 'https'} or self.upstream.username or self.upstream.password:
            raise ValueError('UpstreamConfigurationInvalid')
        self.lock = threading.RLock()
        self.ledger = json.loads(self.path.read_text()) if self.path.exists() else {
            'schema': 1, 'package_id': policy['package_id'], 'policy_sha256': hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(), 'bound_run_id': None,
            'reserved_forward_slots': 0, 'receipts': [], 'denial_counts': {}}
        if self.ledger['package_id'] != policy['package_id'] or self.ledger.get('policy_sha256') != hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest():
            raise ValueError('LedgerPackageMismatch')
        self._persist()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.writing')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(self.ledger, out, sort_keys=True)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)

    def deny(self, reason):
        with self.lock:
            self.ledger['denial_counts'][reason] = self.ledger['denial_counts'].get(reason, 0) + 1
            self._persist()

    def admit(self, body):
        """Reserve before send; durable unknown sends never release a slot."""
        if not isinstance(body, dict) or set(body) != {'caller', 'target'}:
            return 400, 'typed_request_invalid', None
        caller, target = body['caller'], body['target']
        if not isinstance(caller, dict) or not CALLER_REQUIRED <= caller.keys() or not set(caller) <= CALLER_REQUIRED | CALLER_OPTIONAL:
            return 400, 'typed_caller_invalid', None
        if not isinstance(target, dict) or set(target) != TARGET_KEYS:
            return 400, 'typed_target_invalid', None
        for name in ('invocation_id', 'agent_run_id'):
            try: uuid.UUID(caller[name])
            except (ValueError, TypeError, AttributeError): return 400, 'typed_identifier_invalid', None
        index = caller['call_index']
        if type(index) is not int or index < 1:
            return 400, 'typed_call_index_invalid', None
        for key, expected in self.policy['caller'].items():
            if caller.get(key) != expected:
                return 403, 'caller_scope_denied', None
        if target['provider'] != self.policy['provider'] or target['model'] != self.policy['model']:
            return 403, 'model_scope_denied', None
        params = target['model_parameters']
        if not isinstance(params, dict) or type(params.get('max_tokens')) is not int or not 1 <= params['max_tokens'] <= self.policy['max_output_tokens']:
            return 403, 'output_cap_denied', None
        if set(params) - set(self.policy['allowed_parameter_names']):
            return 403, 'unreviewed_parameter_denied', None
        if target['stream'] is not True:
            return 400, 'stream_contract_denied', None
        prompts = target['prompt_messages']
        if not isinstance(prompts, list) or not all(isinstance(x, dict) for x in prompts):
            return 400, 'typed_prompt_invalid', None
        rendered_chars = len(json.dumps(prompts, ensure_ascii=False))
        if rendered_chars > self.policy['prompt_json_char_limit']:
            return 403, 'input_budget_denied', None
        tools = target['tools']
        if tools is not None and (not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('name') not in self.policy['allowed_tool_names'] for t in tools)):
            return 403, 'unreviewed_tool_denied', None
        with self.lock:
            if caller['invocation_id'] in {r['invocation_id'] for r in self.ledger['receipts']}:
                return 409, 'duplicate_invocation_denied', None
            bound = self.ledger['bound_run_id']
            if bound is not None and bound != caller['agent_run_id']:
                return 403, 'different_run_denied', None
            if self.ledger['reserved_forward_slots'] >= self.policy['forward_limit']:
                return 429, 'forward_budget_exhausted', None
            expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dify-agent:{caller['agent_run_id']}:llm:{index}"))
            if caller['invocation_id'] != expected:
                return 403, 'invocation_identity_denied', None
            self.ledger['bound_run_id'] = caller['agent_run_id']
            self.ledger['reserved_forward_slots'] += 1
            receipt = {'slot': self.ledger['reserved_forward_slots'], 'invocation_id': caller['invocation_id'],
                       'agent_run_id': caller['agent_run_id'], 'call_index': index,
                       'max_tokens': params['max_tokens'], 'prompt_json_chars': rendered_chars,
                       'request_sha256': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                       'outcome': 'reserved_before_send'}
            self.ledger['receipts'].append(receipt)
            self._persist()
            return 200, 'admitted', receipt['slot']

    def complete(self, slot, outcome, **safe_fields):
        with self.lock:
            row = next(r for r in self.ledger['receipts'] if r['slot'] == slot)
            row.update(outcome=outcome, **safe_fields)
            self._persist()


def server_for(state, host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def error_response(self, status, reason):
            body = json.dumps({'code': 'acceptance_guard_rejected', 'reason': reason}).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self): self.error_response(403, 'path_not_enabled')
        def do_POST(self):
            if self.path != LLM_PATH:
                return self.error_response(403, 'path_not_enabled')
            # Compare private fixture transport key; never log it.
            import hmac
            if not hmac.compare_digest(self.headers.get('X-Inner-Api-Key', ''), state.key):
                state.deny('transport_key_denied'); return self.error_response(403, 'transport_key_denied')
            try: size = int(self.headers.get('Content-Length', '0'))
            except ValueError: return self.error_response(400, 'typed_length_invalid')
            if size <= 0 or size > state.policy['max_body_bytes']:
                return self.error_response(413, 'body_budget_denied')
            raw = self.rfile.read(size)
            try: body = json.loads(raw)
            except (UnicodeError, ValueError): return self.error_response(400, 'typed_json_invalid')
            status, reason, slot = state.admit(body)
            if slot is None:
                state.deny(reason); return self.error_response(status, reason)
            response_started = False
            connection = None
            try:
                connection_type = http.client.HTTPSConnection if state.upstream.scheme == 'https' else http.client.HTTPConnection
                connection = connection_type(state.upstream.hostname, state.upstream.port, timeout=state.policy['upstream_timeout_seconds'])
                headers = {k:v for k,v in self.headers.items() if k.lower() not in HOP_HEADERS | {'host'}}
                # Keep the genuine authenticated header and original request bytes.
                connection.request('POST', state.upstream.path.rstrip('/') + LLM_PATH, body=raw, headers=headers)
                upstream = connection.getresponse()
                self.send_response(upstream.status)
                for k,v in upstream.getheaders():
                    if k.lower() not in HOP_HEADERS:
                        self.send_header(k,v)
                self.send_header('Connection','close'); self.end_headers(); response_started = True
                digest = hashlib.sha256(); count = 0
                while True:
                    chunk = upstream.read1(65536)
                    if not chunk: break
                    digest.update(chunk); count += len(chunk)
                    self.wfile.write(chunk); self.wfile.flush()
                state.complete(slot, 'upstream_stream_complete', upstream_status=upstream.status,
                               response_bytes=count, response_sha256=digest.hexdigest())
            except Exception as error:
                # Do not release a slot or repeat a possibly sent request.
                state.complete(slot, 'unknown_send_or_stream_outcome', error_class=type(error).__name__)
                if not response_started:
                    self.error_response(502, 'upstream_outcome_unknown')
            finally:
                self.close_connection = True
                if connection: connection.close()
    return ThreadingHTTPServer((host,port), Handler)


def main():
    policy = json.loads(Path(os.environ['O02_GUARD_POLICY_FILE']).read_text())
    state = GuardState(policy, os.environ['O02_GUARD_INNER_KEY'],
                       os.environ['O02_GUARD_LEDGER_FILE'], os.environ['O02_GUARD_UPSTREAM'])
    server_for(state, os.environ.get('O02_GUARD_BIND','127.0.0.1'), int(os.environ.get('O02_GUARD_PORT','8080'))).serve_forever()

if __name__ == '__main__': main()
