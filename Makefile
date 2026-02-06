.PHONY: wheel packages deb rpm clean test

VERSION := $(shell python3 -c "import re; f=open('src/zpbs_backup/__init__.py').read(); print(re.search(r'__version__\s*=\s*\"(.*?)\"', f).group(1))")

wheel:
	python3 -m build --wheel

packages:
	bash packaging/build-packages.sh

deb:
	ARCHES=amd64 bash packaging/build-packages.sh
	@echo "Built: dist/zpbs-backup_$(VERSION)_amd64.deb"

rpm:
	ARCHES=amd64 bash packaging/build-packages.sh
	@echo "Built: dist/zpbs-backup-$(VERSION).x86_64.rpm"

test:
	pytest

clean:
	rm -rf build/ dist/ src/*.egg-info
