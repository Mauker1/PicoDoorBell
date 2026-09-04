####################################################################################
# Pin assignments for the PicoDoorBell carrier board, revision V1.2.
#
# Copy this file to the device as `board.py`. main.py imports it by that name and
# refuses to start without it -- see docs/ARCHITECTURE.md.
####################################################################################

# GPIO wired to the optocoupler output carrying the doorbell signal.
doorBellPin = 16