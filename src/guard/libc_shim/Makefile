CC ?= cc
CFLAGS ?= -O2 -g -fPIC -Wall -Wextra -Wno-unused-parameter
UNAME_S := $(shell uname -s)

ifeq ($(UNAME_S),Darwin)
LDFLAGS ?= -dynamiclib -lpthread
else
LDFLAGS ?= -shared -Wl,--no-as-needed -ldl -lpthread
endif

# Output goes to build/ relative to ARA root. From this dir, ../../../build.
OUT_DIR ?= ../../../build
OUT ?= $(OUT_DIR)/libara_shim.so

SRCS = shim.c ipc.c rules.c canonical.c
OBJS = $(SRCS:.c=.o)

.PHONY: all clean

all: $(OUT)

$(OUT): $(OBJS)
	@mkdir -p $(OUT_DIR)
	$(CC) $(LDFLAGS) -o $@ $(OBJS)

%.o: %.c shim.h
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(OUT)
