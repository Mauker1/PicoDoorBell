####################################################################################
# Pin assignments for the bench prototype board.
#
# Differs from the production carrier only in the doorbell input pin. Copy this
# file to the bench unit as `board.py` so it runs byte-identical firmware.
####################################################################################

# GPIO wired to the optocoupler output carrying the doorbell signal.
doorBellPin = 18