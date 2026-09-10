"""Enable the USB data serial channel, separate from the REPL console.

Without this there is one serial channel and CircuitPython's console shares it,
so a traceback or a stray print lands in the middle of the marker stream. Marks
are data; the console is not. They get their own channel.

Drop this on CIRCUITPY next to code.py and power-cycle the board — boot.py runs
only at reset, not on a soft reload.
"""

import usb_cdc

usb_cdc.enable(console=True, data=True)
