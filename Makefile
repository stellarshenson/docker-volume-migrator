.DEFAULT_GOAL := help
.PHONY: help install build check publish publish-test clean version tag

# Version is single-sourced from the script's VERSION constant.
VERSION := $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' docker_volume_toolkit.py)

## install for local development (editable)
install:
	pip install -e .

## build sdist + wheel into dist/
build: clean
	uv build

## validate built artifact metadata (twine check)
check:
	uvx twine check dist/*

## upload dist/* to PyPI (needs UV_PUBLISH_TOKEN or ~/.pypirc)
publish:
	uv publish

## upload dist/* to TestPyPI
publish-test:
	uv publish --publish-url https://test.pypi.org/legacy/

## remove build artifacts
clean:
	rm -rf dist build *.egg-info

## print the package version
version:
	@echo $(VERSION)

## create git tag v$(VERSION)
tag:
	git tag v$(VERSION)

## prints available commands
help:
	@echo ""
	@echo "$$(tput bold)Available rules:$$(tput sgr0)"
	@sed -n -e "/^## / { \
		h; \
		s/.*//; \
		:doc" \
		-e "H; \
		n; \
		s/^## //; \
		t doc" \
		-e "s/:.*//; \
		G; \
		s/\\n## /---/; \
		s/\\n/ /g; \
		p; \
	}" ${MAKEFILE_LIST} \
	| LC_ALL='C' sort --ignore-case \
	| awk -F '---' \
		-v ncol=$$(tput cols) \
		-v indent=19 \
		-v col_on="$$(tput setaf 6)" \
		-v col_off="$$(tput sgr0)" \
	'{ \
		printf "%s%*s%s ", col_on, -indent, $$1, col_off; \
		n = split($$2, words, " "); \
		line_length = ncol - indent; \
		for (i = 1; i <= n; i++) { \
			line_length -= length(words[i]) + 1; \
			if (line_length <= 0) { \
				line_length = ncol - indent - length(words[i]) - 1; \
				printf "\n%*s ", -indent, " "; \
			} \
			printf "%s ", words[i]; \
		} \
		printf "\n"; \
	}'
	@echo ""
