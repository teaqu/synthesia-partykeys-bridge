"""Read PartyKeys topology and assign a routing channel to an unassigned main."""
import logging
import time
import mido

LOG = logging.getLogger("partykeys")


def report(message):
    if message.type != "sysex":
        return None
    data = message.data
    if (len(data) < 8 or data[0] not in (5,0x7f) or data[1] != 0x30
            or data[2:4] not in ((0x7e,0x7e),(0x20,0))):
        return None
    channel, address, command, value = data[4:8]
    return (channel,address,command,value) if channel <= 15 and address <= 0x6f else None


class BoardWatcher:
    """Debounced join/remove queries without sleeping on the MIDI thread."""
    def __init__(self, boards, octaves=None, now=time.monotonic):
        self.boards, self.octaves, self.now = list(boards), dict(octaves or {}), now
        self.query_at = now() + 5
        self.pending = None
        self.deadline = None
        self.suspended = False
        self.dirty = False
        self.assignment = None
        self.allocated = set()

    def request(self):
        self.query_at = self.now() + .7
        self.pending = None
        self.deadline = None
        self.assignment = None
        self.allocated.clear()
        self.suspended = self.dirty = True

    def receive(self, message):
        decoded = report(message)
        if decoded is None:
            return False
        channel, address, command, value = decoded
        if command in (4,5):
            self.request()
            return True
        if command == 0x18 and channel:
            octave = value - 128 if value >= 64 else value
            if -3 <= octave <= 3:
                self.octaves[channel] = octave
            return False
        if command != 2:
            return False
        if self.pending is None:
            if not any((ch,addr) == (channel,address) for ch,addr,_ in self.boards):
                self.request()
            return False
        if channel == 0 and value == 1 and self.assignment is None and address not in self.allocated:
            used = {ch for ch,_,_ in self.boards} | {ch for ch,_ in self.pending}
            chosen = next((ch for ch in range(1,16) if ch not in used), None)
            if chosen is not None:
                self.assignment = chosen
                self.allocated.add(address)
                self.suspended = True
        elif channel:
            low = 48 + (address - 0x3f) * 36
            if 0 <= low <= 92:
                self.pending[(channel,address)] = (channel,address,low)
                if (channel,address,low) not in self.boards:
                    self.suspended = True
        return False

    def tick(self, driver):
        now = self.now()
        if self.pending is not None and now >= self.deadline:
            result = sorted(self.pending.values(), key=lambda board: board[2])
            self.pending, self.deadline, self.assignment = None, None, None
            self.query_at = now + 5
            if not result:
                # Missing replies alone do not prove every board disconnected.
                # After an explicit event, keep writes suspended until recovery.
                if self.suspended:
                    LOG.warning("Waiting for keyboard topology reports; addressed LED writes suspended")
                return None
            reinitialize = self.dirty
            changed = result != self.boards or reinitialize
            self.boards, self.suspended, self.dirty = result, False, False
            return (result, dict(self.octaves), reinitialize) if changed else None
        if now < driver.last_send + driver.min_interval:
            return None
        if self.pending is None and now >= self.query_at:
            driver._send(mido.Message("sysex",data=(5,0x30,0x7f,0x7f,0x20,0,1)))
            self.pending, self.deadline = {}, self.now() + 3
            self.allocated.clear()
            return None
        if self.pending is not None and self.assignment is not None:
            driver._send(mido.Message("sysex",data=(5,0x30,0,0x7f,0x20,0,3,self.assignment)))
            self.assignment = None
            self.deadline = self.now() + 3
        return None


def discover_boards(source, output, now=time.monotonic, sleep=time.sleep):
    output.send(mido.Message("sysex", data=(5,0x30,0x7f,0x7f,0x20,0,1)))
    boards, octaves, assigned = {}, {}, False
    deadline = now() + 3
    while now() < deadline:
        for message in source.iter_pending():
            if message.type != "sysex":
                continue
            data = message.data
            if len(data) < 8 or data[0] not in (5,0x7f) or data[1] != 0x30 or data[2:4] not in ((0x7e,0x7e),(0x20,0)):
                continue
            channel, address, command, value = data[4:8]
            if channel > 15 or address > 0x6f:
                continue
            if command == 2 and channel == 0 and value == 1 and not assigned:
                # Both joined boards follow their main keyboard's channel.
                used = {ch for ch, _ in boards}
                chosen = next(ch for ch in range(1,16) if ch not in used)
                output.send(mido.Message("sysex",data=(5,0x30,0,0x7f,0x20,0,3,chosen)))
                assigned = True
                deadline = now() + 3
            elif command == 2 and channel:
                low = 48 + (address - 0x3f) * 36
                if 0 <= low <= 92:
                    boards[(channel,address)] = (channel,address,low)
            elif command == 0x18 and channel:
                octave = value - 128 if value >= 64 else value
                if -3 <= octave <= 3:
                    octaves[channel] = octave
        sleep(.01)
    if not boards:
        raise OSError("No addressed PartyKeys board reports received; refusing unaddressed lighting")
    result = sorted(boards.values(), key=lambda board: board[2])
    LOG.info("Discovered PartyKeys board routes: %s", result)
    return result, octaves
