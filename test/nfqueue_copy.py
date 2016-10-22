import nfqueue
import sys

queue = 1

conn = nfqueue.Connection(alloc_size=int(sys.argv[1]), chunk_size=int(sys.argv[2]))

try:
    q = conn.bind(queue)
    q.set_mode(0xffff, nfqueue.COPY_PACKET)
except PermissionError:
    print("Access denied; Do I have root rights or the needed capabilities?")
    sys.exit(-1)

print("run", flush=True)

while True:
    try:
        for packet in conn:
            packet.payload = packet.payload
            packet.accept(nfqueue.MANGLE_PAYLOAD)
    except nfqueue.BufferOverflowException:
        print("buffer error")
        pass

conn.close()
