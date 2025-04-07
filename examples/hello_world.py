from socket import gethostname
import time
flush = True
print("Executing hello_world.py on", gethostname(), flush=flush)
print("Hello world!", flush=flush)
time.sleep(5)
print("Hello again!", flush=flush)