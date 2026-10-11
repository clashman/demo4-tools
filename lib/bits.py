"""Bit reader matching Unreal Engine 2's FBitReader (LSB-first)."""
import struct


class BitError(Exception):
    pass


class BitReader:
    __slots__ = ('buf', 'pos', 'num')

    def __init__(self, buf, num_bits=None, pos=0):
        self.buf = buf
        self.pos = pos
        self.num = len(buf) * 8 if num_bits is None else num_bits

    def at_end(self):
        return self.pos >= self.num

    def left(self):
        return self.num - self.pos

    def bit(self):
        if self.pos >= self.num:
            raise BitError('read past end')
        b = (self.buf[self.pos >> 3] >> (self.pos & 7)) & 1
        self.pos += 1
        return b

    def bits(self, n):
        """Read n bits as an unsigned little-endian integer."""
        if self.pos + n > self.num:
            raise BitError('read past end')
        v = 0
        for i in range(n):
            p = self.pos + i
            if (self.buf[p >> 3] >> (p & 7)) & 1:
                v |= 1 << i
        self.pos += n
        return v

    def read_int(self, vmax):
        """FBitReader::SerializeInt(Value, ValueMax)."""
        v = 0
        mask = 1
        while v + mask < vmax and mask:
            if self.pos >= self.num:
                raise BitError('read past end')
            if (self.buf[self.pos >> 3] >> (self.pos & 7)) & 1:
                v |= mask
            self.pos += 1
            mask <<= 1
        return v

    def byte(self):
        return self.bits(8)

    def i32(self):
        v = self.bits(32)
        return v - (1 << 32) if v & 0x80000000 else v

    def u16(self):
        return self.bits(16)

    def i16(self):
        v = self.bits(16)
        return v - 0x10000 if v & 0x8000 else v

    def f32(self):
        return struct.unpack('<f', struct.pack('<I', self.bits(32)))[0]

    def compact_index(self):
        b = self.byte()
        neg = b & 0x80
        v = b & 0x3F
        if b & 0x40:
            sh = 6
            while True:
                b = self.byte()
                v |= (b & 0x7F) << sh
                sh += 7
                if not (b & 0x80) or sh > 27:
                    break
        return -v if neg else v

    def fstring(self):
        n = self.compact_index()
        if n == 0:
            return ''
        if n > 0:
            if n > 4096:
                raise BitError('string too long')
            s = bytes(self.byte() for _ in range(n))
            return s[:-1].decode('latin1')
        n = -n
        if n > 4096:
            raise BitError('string too long')
        s = bytes(self.byte() for _ in range(2 * n))
        return s[:-2].decode('utf-16-le', 'replace')

    def sub(self, nbits):
        """Return a reader over the next nbits and advance past them."""
        if self.pos + nbits > self.num:
            raise BitError('sub past end')
        r = BitReader(self.buf, self.pos + nbits, self.pos)
        self.pos += nbits
        return r


def packet_bits(data):
    """Number of payload bits in a packet (trailing 1-bit terminator)."""
    last = data[-1]
    if last == 0:
        raise BitError('bad packet terminator')
    hb = last.bit_length() - 1
    return (len(data) - 1) * 8 + hb
