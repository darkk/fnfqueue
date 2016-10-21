from _nfqueue import ffi, lib
import threading
import os
import errno
import collections
import itertools
try:
    import Queue as queue
except:
    import queue

COPY_NONE = lib.NFQNL_COPY_NONE
COPY_META = lib.NFQNL_COPY_META
COPY_PACKET = lib.NFQNL_COPY_PACKET

DROP = lib.NF_DROP
ACCEPT = lib.NF_ACCEPT
STOLEN = lib.NF_STOLEN
QUEUE = lib.NF_QUEUE
REPEAT = lib.NF_REPEAT
STOP = lib.NF_STOP

MANGLE_MARK = lib.MANGLE_MARK
MANGLE_PAYLOAD = lib.MANGLE_PAYLOAD
MANGLE_CT = lib.MANGLE_CT
MANGLE_EXP = lib.MANGLE_EXP
MANGLE_VLAN = lib.MANGLE_VLAN

MAX_PAYLOAD = 0xffff

# FAIL_OPEN  # 0: drop 1: accept on error
# CONNTRACK  # report conntrack
# GSO        # support gso
# UID_GID    # report UID GID of process
# SECCTX     # report security context


class PacketInvalidException(Exception):
    pass

class PayloadTruncatedException(Exception):
    pass

class BufferOverflowException(Exception):
    pass

class Packet:
    def __init__(self, conn, p):
        self.cache = {}
        self.packet = p
        self._conn = conn
        self._mangle = 0
        self._invalid = False

    def mangle(self):
        self.accept(self._mangle)

    def accept(self, mangle=0):
        self.verdict(ACCEPT, mangle)

    def drop(self):
        self.verdict(DROP, 0)

    def verdict(self, action, mangle=0):
        self._is_invalid()
        if mangle & lib.MANGLE_PAYLOAD:
            b = ffi.new("char []", self.cache['payload'])
            self.packet.attr[lib.NFQA_PAYLOAD].buffer = b
            self.packet.attr[lib.NFQA_PAYLOAD].len = len(self.cache['payload'])
        if self._conn._conn is not None:
            ret = lib.set_verdict(self._conn._conn, self.packet, action, mangle)
        self._conn.recycle(self.packet)
        self._invalidate()
        if ret == -1:
            raise OSError(ffi.errno, os.strerror(ffi.errno))

    def _invalidate(self):
        del self.cache
        del self.packet
        del self._conn
        self._invalid = True

    def _is_invalid(self):
        if self._invalid:
            raise PacketInvalidException()

    @property
    def payload(self):
        self._is_invalid()
        if 'payload' in self.cache:
            return self.cache['payload']
         #FIXME: check if there is actually apayload
        if self.packet.attr[lib.NFQA_CAP_LEN].buffer != ffi.NULL:
            raise PayloadTruncatedException()
        #change that to a custom buffer later
        self.cache['payload'] = ffi.unpack(ffi.cast("char *",
                        self.packet.attr[lib.NFQA_PAYLOAD].buffer),
                        self.packet.attr[lib.NFQA_PAYLOAD].len)
        return self.cache['payload']

    @payload.setter
    def payload(self, value):
        self._is_invalid()
        self.cache['payload'] = value
        self._mangle |= lib.MANGLE_PAYLOAD

    @payload.deleter
    def payload(self):
        self._is_invalid()
        self._mangle &= ~lib.MANGLE_PAYLOAD
        self.cache['payload'] = None

    @property
    def hw_protocol(self):
        self._is_invalid()
        return self.packet.hw_protocol

    @property
    def hook(self):
        self._is_invalid()
        return self.packet.hook

    #TODO: add attributes:
    #MARK get set
    #TIMESTAMP get
    #IFINDEX_INDEV get
    #IFINDEX_OUTDEV get
    #IFINDEX_PHYSINDEV get
    #IFINDEX_PHYSOUTDEV get
    #HWADDR get
    #CT get set
    #CT_INFO get
    #CAP_LEN get
    #SKB_INFO get
    #EXP set
    #UID get
    #GID get
    #SECCTX get
    #VLAN get set
    #L2HDR get


class Connection:
    def __init__(self, alloc_size = 50, chunk_size = 10, packet_size = 20*4096): # just a guess for now
        self.alloc_size = alloc_size
        self.chunk_size = chunk_size
        self.packet_size = packet_size
        self._conn = ffi.new("struct nfq_connection *");
        lib.init_connection(self._conn)
        self._buffers = collections.deque()
        self._packets = collections.deque()
        self._packet_lock = threading.Lock()
        self._received = queue.Queue()
        self._worker = threading.Thread(target=self._reader)
        self._worker.daemon = True
        self._worker.start()
        #try custom queue implementation

    def _reader(self):
        chunk_size = self.chunk_size
        packets = ffi.new("struct nfq_packet*[]", chunk_size)
        while self._conn is not None:
            with self._packet_lock:
                if len(self._packets) < chunk_size:
                    self._alloc_buffers()
                packets[0:chunk_size] = itertools.islice(self._packets, chunk_size)
            num = lib.receive(self._conn, packets, chunk_size)
            if num == -1:
                if ffi.errno == errno.ENOBUFS:
                    self._received.put(BufferOverflowException())
                    continue
                else:
                    #SMELL: be more graceful?
                    #handle wrong filedescriptor for closeing connection
                    self._received.put(OSError(ffi.errno, os.strerror(ffi.errno)))
                    return
            with self._packet_lock:
                for i in range(num):
                    self._received.put(self._packets.popleft())

    def _alloc_buffers(self):
        for i in range(self.alloc_size):
            packet = ffi.new("struct nfq_packet *");
            b = ffi.new("char []", self.packet_size)
            self._buffers.append(b)
            packet.buffer = b
            packet.len = self.packet_size
            self._packets.append(packet)

    def recycle(self, packet):
        with self._packet_lock:
            self._packets.append(packet)

    def __iter__(self):
        while True:
            p = self._received.get()
            if isinstance(p, Exception):
                raise p
            if isinstance(p, Packet):
                yield p
            err = lib.parse_packet(p)
            if err != 0:
                raise Exception('Hmm: {} {} {}'.format(p.seq, err, os.strerror(err)))
            else:
                yield Packet(self, p)
        
    def bind(self, queue):
        ret = lib.bind_queue(self._conn, queue)
        if ret == -1:
            raise OSError(ffi.errno, os.strerror(ffi.errno))

    def unbind(self, queue):
        ret = lib.unbind_queue(self._conn, queue)
        if ret == -1:
            raise OSError(ffi.errno, os.strerror(ffi.errno))

    def set_mode(self, queue, size, mode):
        ret = lib.set_mode(self._conn, queue, size, mode)
        if ret == -1:
            raise OSError(ffi.errno, os.strerror(ffi.errno))

    #flags
    #maxlen
    #change rcvbuffer

    def close(self):
        conn = self._conn
        self._conn = None
        lib.close_connection(conn)
