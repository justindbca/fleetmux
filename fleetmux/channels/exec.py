"""exec push channel (P6a; contracts §8 "exec 推送通道", brief v2 §2.2 P6a, approve-1 test-plan R1 / R2 / R12).

Outbound only. Each outbound item goes, as one JSON object (``envelope.dumps``), to the stdin of the command in the
account file's ``COMMAND=``. The operator's own relay (a phone push service, a webhook client) picks it up from
there.

  * ``COMMAND=`` -> argv by ``channels.exec_argv`` (``shlex.split``), run with ``shell=False``: no pipes, ``;``,
    ``$VAR``, ``~`` or globs. ``TIMEOUT_S=`` by ``channels.exec_timeout`` (default 30). The child inherits the
    daemon's environment; no other account value is put into its argv or environment.
  * ``transport(argv, stdin_bytes, timeout) -> (returncode, stdout_text)``; None = ``subprocess.run``.
  * Exit 0 = sent. The first stdout line is the message id (``{"message_id": ...}`` JSON or plain text). Non-zero
    exit, a timeout, a command that cannot start or a bad ``COMMAND`` -> ``SendResult(ok=False)``; never a raise.
    The error never echoes ``COMMAND`` or the child's output (either may hold a relay URL or a token).
  * ``RECEIVES = False``; ``SUPPORTS_CARDS = SUPPORTS_FILES = True``; ``SUPPORTS_BUTTONS = SUPPORTS_UPDATE =
    False``; ``CARRIES_TOKENS = False``: every card is redacted (no buttons, no ref) before it leaves, and a
    ``user_only`` item (a delete approval) is refused before any child process starts.
  * ``send_item(item)`` writes the queue item itself. ``send_text / send_card / send_file`` (and ``update_card``,
    which becomes a new card) are wrapped into ``event: manual`` items with ``target: explicit``.
"""
import json
import subprocess

from fleetmux import channels
from fleetmux.channels import base, envelope


def run_command(argv, stdin, timeout):
    """The real transport: the child gets ``stdin`` and is killed after ``timeout`` s (subprocess.run)."""
    done = subprocess.run(list(argv), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=timeout, shell=False, check=False)
    return done.returncode, (done.stdout or b'').decode('utf-8', 'replace')


def message_id(stdout):
    """First stdout line: ``{"message_id": ...}`` JSON, else the line itself; empty -> None."""
    line = (str(stdout or '').splitlines() or [''])[0].strip()
    if not line:
        return None
    if line.startswith('{'):
        try:
            data = json.loads(line)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get('message_id') not in (None, ''):
            return str(data['message_id'])[:200]
    return line[:200]


class ExecFailed(Exception):
    """A send that did not succeed. The message is fixed text: no COMMAND, no stdout / stderr."""


class ADAPTER(base.Channel):
    type = 'exec'
    RECEIVES = False
    SUPPORTS_CARDS = True
    SUPPORTS_BUTTONS = False
    SUPPORTS_UPDATE = False
    SUPPORTS_FILES = True
    CARRIES_TOKENS = False

    # ---------------------------------------------------------------- the one way out
    def _argv(self):
        try:
            return channels.exec_argv(self.account.values.get('COMMAND', ''))
        except ValueError:
            # exec_argv's message is safe, but it would still name the problem with the quotes. Keep it fixed.
            raise ExecFailed('COMMAND in the account file is missing or not a valid command line') from None

    def _timeout(self):
        try:
            return channels.exec_timeout(self.account.values.get('TIMEOUT_S'))
        except ValueError:
            raise ExecFailed('TIMEOUT_S in the account file must be a positive number of seconds') from None

    def _run(self, item):
        """Hand one item to the command. Returns the message id; raises ExecFailed / the transport's error."""
        problem = envelope.outbound_problem(item)
        if problem:
            raise ExecFailed('not a valid outbound item: ' + problem)
        argv, timeout = self._argv(), self._timeout()
        transport = self.transport or run_command
        try:
            rc, stdout = transport(argv, envelope.dumps(item).encode('utf-8'), timeout)
        except subprocess.TimeoutExpired:
            raise ExecFailed('command timed out after ' + format(timeout, 'g') + ' s') from None
        except FileNotFoundError:
            raise ExecFailed('command not found') from None
        except PermissionError:
            raise ExecFailed('command not executable') from None
        if rc != 0:
            raise ExecFailed('command exited with ' + str(rc))
        return message_id(stdout)

    def _manual(self, conversation, reply_ref, **body):
        return envelope.outbound('manual', target='explicit', channel=self.type, account=self.account.name,
                                 conversation=conversation, reply_ref=reply_ref if isinstance(reply_ref, dict)
                                 else None, request_id=str(self.send_ref or ''), **body)

    # ---------------------------------------------------------------- base hooks
    def _send_text(self, conversation, text, reply_ref):
        return self._run(self._manual(conversation, reply_ref, text=text))

    def _send_card(self, conversation, card, reply_ref):
        return self._run(self._manual(conversation, reply_ref, card=envelope.redact_card(card)))

    def _send_file(self, conversation, path, caption, reply_ref):
        return self._run(self._manual(conversation, reply_ref, file=str(path), caption=caption or ''))

    # ---------------------------------------------------------------- the actor's entry
    def send_item(self, item):
        """Send one outbound queue item as is (the actor calls this for exec, brief v2 §2.2 P6a). Never raises.
        ``user_only`` items are refused without starting the command; a card goes out redacted."""
        try:
            if not isinstance(item, dict):
                raise ExecFailed('not a valid outbound item')
            if item.get('user_only'):
                raise ExecFailed('user_only item: exec never carries approvals')
            if isinstance(item.get('card'), dict) and not envelope.card_problem(item['card']):
                item = dict(item, card=envelope.redact_card(item['card']))
            got = base.SendResult(ok=True, message_id=self._run(item))
        except Exception as exc:
            return self._fail('send_item', exc)
        self.last_error = ''
        return got

    def _fail(self, what, exc):
        if isinstance(exc, ExecFailed):
            self.last_error = self.redact(what + ': ' + str(exc))[:500]
            self.audit('channel.error', error=self.last_error, kind='')
            return base.SendResult(ok=False, error=self.last_error)
        if isinstance(exc, OSError):   # e.g. ENOEXEC from a real spawn: keep the errno text, never the filename
            exc = OSError(exc.strerror or type(exc).__name__)
        return super()._fail(what, exc)


ExecChannel = ADAPTER
