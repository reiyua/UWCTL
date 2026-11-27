# uwctl - Linux control centre for Uniwill-based laptops
#
#   make install DESTDIR=/tmp/pkg PREFIX=/usr
#   make test
#   make deb          (Debian / Ubuntu package, needs dpkg-deb)
#   make dist         (source tarball for the Arch PKGBUILD)

VERSION   := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/uwctl/__init__.py)
PREFIX    ?= /usr
DESTDIR   ?=
LIBDIR    ?= $(PREFIX)/lib/uwctl
BINDIR    ?= $(PREFIX)/bin
UNITDIR   ?= $(PREFIX)/lib/systemd/system
SLEEPDIR  ?= $(PREFIX)/lib/systemd/system-sleep
SYSUSERS  ?= $(PREFIX)/lib/sysusers.d
MODLOAD   ?= $(PREFIX)/lib/modules-load.d
APPDIR    ?= $(PREFIX)/share/applications
ICONDIR   ?= $(PREFIX)/share/icons/hicolor/scalable/apps
DOCDIR    ?= $(PREFIX)/share/doc/uwctl
PYTHON    ?= python3
APPID     := io.github.uwctl.ControlCenter

.PHONY: all install uninstall test deb dist clean

all:
	@echo "uwctl $(VERSION): nothing to build. Try 'make install', 'make test', 'make deb' or 'make dist'."

install:
	install -d $(DESTDIR)$(LIBDIR)/uwctl/gui
	install -m644 src/uwctl/*.py $(DESTDIR)$(LIBDIR)/uwctl/
	install -m644 src/uwctl/gui/*.py $(DESTDIR)$(LIBDIR)/uwctl/gui/
	install -d $(DESTDIR)$(BINDIR)
	for b in uwctl uwctld uwctl-gui; do \
		sed 's|@LIBDIR@|$(LIBDIR)|' bin/$$b > $(DESTDIR)$(BINDIR)/$$b; \
		chmod 755 $(DESTDIR)$(BINDIR)/$$b; \
	done
	install -Dm644 data/uwctld.service $(DESTDIR)$(UNITDIR)/uwctld.service
	install -Dm755 data/uwctl-sleep $(DESTDIR)$(SLEEPDIR)/uwctl
	install -Dm644 data/uwctl.sysusers $(DESTDIR)$(SYSUSERS)/uwctl.conf
	install -Dm644 data/uwctl-modules.conf $(DESTDIR)$(MODLOAD)/uwctl.conf
	install -Dm644 data/$(APPID).desktop $(DESTDIR)$(APPDIR)/$(APPID).desktop
	install -Dm644 data/$(APPID).svg $(DESTDIR)$(ICONDIR)/$(APPID).svg
	install -Dm644 README.md $(DESTDIR)$(DOCDIR)/README.md

uninstall:
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(BINDIR)/uwctl $(DESTDIR)$(BINDIR)/uwctld $(DESTDIR)$(BINDIR)/uwctl-gui
	rm -f $(DESTDIR)$(UNITDIR)/uwctld.service $(DESTDIR)$(SLEEPDIR)/uwctl
	rm -f $(DESTDIR)$(SYSUSERS)/uwctl.conf $(DESTDIR)$(MODLOAD)/uwctl.conf
	rm -f $(DESTDIR)$(APPDIR)/$(APPID).desktop $(DESTDIR)$(ICONDIR)/$(APPID).svg
	rm -rf $(DESTDIR)$(DOCDIR)

test:
	$(PYTHON) -m pytest -q tests

deb:
	packaging/debian/build-deb.sh

dist:
	mkdir -p dist
	git archive --format=tar.gz --prefix=uwctl-$(VERSION)/ -o dist/uwctl-$(VERSION).tar.gz HEAD 2>/dev/null || \
	tar --transform 's,^\.,uwctl-$(VERSION),' --exclude=./dist --exclude=./build --exclude=./.git --exclude='__pycache__' \
	    --exclude=.pytest_cache -czf dist/uwctl-$(VERSION).tar.gz .

clean:
	rm -rf dist build .pytest_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
