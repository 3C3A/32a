windres version.rc version.o
g++ 32a.cpp version.o -o 32a.exe -std=c++17 -O2 -Wall -Wextra -Wpedantic -static -static-libgcc -static-libstdc++ -mwindows -lshell32 -ladvapi32 -luser32 -lgdi32
:: cl /nologo /std:c++17 /O2 /EHsc /DUNICODE /D_UNICODE 32a.cpp /link /SUBSYSTEM:WINDOWS shell32.lib advapi32.lib user32.lib gdi32.lib

32a 32a-demo.32a
pause
