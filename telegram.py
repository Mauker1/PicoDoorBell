####################################################################################
# Telegram: transport, commands, and ring delivery (E2).
#
# Owns the bot API: sendMessage and getUpdates, the update offset, the
# start-up backlog discard, and the error wording Telegram's JSON carries.
# Commands dispatch through a table (command text -> handler(chatId)) that
# main fills at import, so a command needing state from above (/status needs
# the heartbeat) costs no upward import. /log is answered here by
# print_log(), since everything it needs sits below.
#
# Delivery of the ring queue lives here too: flush_queue() sends what
# ringqueue holds and pops an entry only once Telegram confirms it, and
# describe_delay() words each ring's time.
#
# Dependencies: config, applog, net, led, clockmod, ringqueue. Nothing above.
# main calls setup(botToken, chatId) once at import, so it stays the single
# reader of secrets.py, and sets utcOffset for the ring times.
####################################################################################

import time
import config
import applog
import net
import led
import clockmod
import ringqueue

# Set by setup(). The URLs embed the bot token: never print or log them.
chatId = None
sendURL = ''
getURL = ''

# Telegram update id for offset. RAM only; discard_update_backlog() restores
# a sensible value after every reset.
updateId = 0

# Display offset for ring times, in seconds. Set by main at import.
utcOffset = 0

# Command text -> handler(chatId). Filled by main at import.
commands = {}

def setup(botToken, chat):
    """Take the credentials and build the two endpoint URLs, once.

    Built here rather than per request: getUpdates runs every minute, and
    concatenating the URL each time would allocate on every poll.
    """
    global chatId, sendURL, getURL
    chatId = chat
    sendURL = 'https://api.telegram.org/bot' + botToken + '/sendMessage'
    getURL = 'https://api.telegram.org/bot' + botToken + '/getUpdates'

def describe_api_error(status, body):
    # Telegram reports failures as
    # {"ok": false, "error_code": N, "description": "..."}
    detail = ''
    if body is not None:
        try:
            detail = ' ' + str(body.get('description', ''))
        except AttributeError:
            detail = ''
    return 'status=' + str(status) + detail

def retry_after(body):
    # Telegram puts the cooldown in parameters.retry_after on a 429.
    # Not called yet: ROADMAP A11 wires it in, holding delivery on a 429.
    if body is None:
        return 0
    try:
        return int(body['parameters']['retry_after'])
    except (KeyError, TypeError, ValueError):
        return 0


# The watchdog primitive (arm_watchdog, feed_watchdog, sleep_fed) now lives
# in wdt.py.

# Send a telegram message to a given user id
def send_message (chatId, message):
    param = {'chat_id': chatId, 'text': message}
    outcome, status, body = net.do_request('POST', sendURL, param)
    if outcome not in (net.REQUEST_OK, net.REQUEST_SKIPPED) and status != 0:
        # status 0 means do_request already reported the transport error.
        applog.report('sendMessage failed: ' + describe_api_error(status, body))
    return (outcome, status, body)

def discard_update_backlog():
    """Drop anything Telegram has been holding for us.

    `updateId` lives in RAM, so every reset restarts it at zero and
    getUpdates replays the backlog -- re-executing commands sent up to 24
    hours ago. Observed: a `/log` answered again after every reboot.

    An offset of -1 asks for the last update only; acknowledging it clears
    everything before it.
    """
    global updateId
    outcome, status, body = net.do_request('GET', getURL + '?offset=-1&limit=1')
    if outcome != net.REQUEST_OK:
        return False
    try:
        results = body['result']
    except (KeyError, TypeError):
        return False
    if results:
        updateId = results[-1]['update_id'] + 1
        applog.report('Discarded ' + str(len(results)) + ' stale update(s)')
    return True

def read_message(chatId):
    global updateId
    url = ''
    if (updateId != 0):
        url = getURL + "?offset=" + str(updateId) + "?chat_id=" + str(chatId)
    else:
        url = getURL + "?chat_id=" + str(chatId)
    # NOTE: never print or log `url` -- it embeds the bot token.
    outcome, status, body = net.do_request('GET', url)
    if outcome == net.REQUEST_SKIPPED:
        return
    if outcome != net.REQUEST_OK:
        # status 0 means do_request already reported the transport error
        # with its errno; repeating it as 'status=0' adds nothing.
        if status != 0:
            applog.append_to_log('getUpdates failed: ' +
                          describe_api_error(status, body))
        return
    for result in body['result']:
        updateId = result['update_id'] + 1
        command = result['channel_post']['text']
        # Dispatch through the table main fills at import. Unknown commands
        # are ignored, as before.
        handler = commands.get(command)
        if handler is not None:
            handler(chatId)

def print_log(chatId):
    """Send the log, in pieces, and clear it only once it has all landed.

    Telegram caps a message at 4096 characters. The log was allowed to
    reach 10000, so a full log was an unconditional 400 -- and the old code
    then cleared it anyway, destroying the thing that had just failed to
    send. Observed: /log worked early on and stopped once the log filled.

    Stays with the network side: applog holds the buffer and its bound,
    while sending it and deciding when to clear it are this caller's
    policy. The seed line for the cleared log is the WiFi status.
    """
    if not applog.logLines:
        send_message(chatId, 'Log is empty.')
        return True
    pending = applog.log_text()
    while pending:
        chunk = pending[:config.LOG_CHUNK_CHARS]
        if len(pending) > config.LOG_CHUNK_CHARS:
            edge = chunk.rfind('\n')
            if edge > 0:
                chunk = chunk[:edge]
        outcome, status, body = send_message(chatId, chunk)
        if outcome != net.REQUEST_OK:
            # Keep everything. A log that failed to send is exactly the log
            # someone needs.
            applog.report('Log send failed; keeping it')
            return False
        pending = pending[len(chunk):]
        if pending[:1] == '\n':
            pending = pending[1:]
    applog.reset_log(net.wifiData)
    return True

# Tier 2 persistence (state.json) lives in persist.py.

def describe_delay(entry):
    """Trailing note on a delivered ring: when it rang, or how late it is.

    A ring carrying a real epoch shows its wall-clock time, which is what
    C1 exists for. A restored ring's epoch is only coarse (the device may
    have been off between the anchor write and the next boot), so it is
    tagged approximate. Without any epoch the old relative wording stands:
    a restart note, or a delay in seconds once past the notice threshold.
    """
    epoch = entry[ringqueue.Q_EPOCH]
    if entry[ringqueue.Q_RESTORED]:
        if epoch is not None:
            return ' (rang around ' + \
                   clockmod.format_timestamp(epoch, utcOffset) + \
                   ', before a restart)'
        return ' (queued before a restart)'
    if epoch is not None:
        return ' (' + clockmod.format_timestamp(epoch, utcOffset) + ')'
    age = time.ticks_diff(time.ticks_ms(), entry[ringqueue.Q_TICKS])
    if age < config.DELAY_NOTICE_MS:
        return ''
    return ' (delayed ' + str(age // 1000) + 's)'

def flush_queue():
    """Deliver what is waiting. Stops at the first retryable failure.

    Nothing leaves the queue until Telegram has confirmed it. The previous
    code sent and forgot, so a failed send lost the ring silently -- seen
    on the bench as a logged ring that never arrived.
    """
    sentCount = 0
    while ringqueue.queue and sentCount < config.QUEUE_FLUSH_PER_PASS:
        entry = ringqueue.queue[0]
        outcome, status, body = send_message(chatId, config.text + describe_delay(entry))
        if outcome == net.REQUEST_OK:
            ringqueue.queue.pop(0)
            sentCount += 1
        elif outcome == net.REQUEST_FATAL:
            # Retrying will not help. Drop it rather than block the queue
            # behind something permanently undeliverable.
            ringqueue.queue.pop(0)
            applog.report('Dropped an undeliverable ring: ' +
                   describe_api_error(status, body))
        else:
            # Transient or rate limited. Leave it and try again next pass.
            break
    if sentCount:
        # Local confirmation that a ring reached Telegram. Once per pass, not
        # per ring: a burst flushing together is one visitor's worth of
        # feedback, and blinking N times would just be noise.
        led.led_alert()
    return sentCount
