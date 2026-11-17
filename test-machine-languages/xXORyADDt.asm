PASS 0, 0
SHFL 2, 0
PASS 0, 0
SHFL 5, 0
PASS 0, 0

SHFL 1, 0
AND 0, 255

SHFL 2, 0
SHR 0, 8

SHFL 2, 1
XOR 0, 0

; (X XOR Y) + t
SHFL 1, 5
ADD 0, 0

; Red
SHFL 1, 0
SHR 0, 3

SHFL 1, 0
SHL 0, 11

; Green
SHFL 3, 0
SHR 0, 2

SHFL 1, 0
SHL 0, 5

; Blue
SHFL 5, 0
SHR 0, 3

; Combine RGB565
SHFL 4, 2
OR 0, 0

SHFL 1, 2
OR 0, 0

; Draw pixel
SHFL 12, 1
VGA 0, 0

; Increment 16-bit pixel counter
SHFL 12, 0
ADD 0, 1

; Detect counter overflow
SHFL 0, 1
CMP 0, 0

; Increment t only when counter wraps to zero
SHFL 15, 1
ADD 0, 0