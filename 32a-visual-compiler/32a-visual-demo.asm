# 32a Visual Compiler example
# Blue blocks = GPU OP, green blocks = CPU OP.

start:
ADD 10, 20          # result history #1
ADD 1, 2            # result history #2
SHFL 1, 2           # next ADD: A <- latest (block 1), B <- second latest (block 0)
ADD 0, 0

IF_EQ 30, 30        # true: skip the next ADD block
ADD 99, 1
JUMP ->done         # drag the left arrow to change this target visually
ADD 77, 77

done:
TEXT 0x0000, 0xFF41 # character A at (0,0), palette index 255
WAIT 150
HALT
