PYTHON ?= python3

.PHONY: test

test:
	./git_viewspace_fs/tests/make_test_repo.sh
	$(PYTHON) -m unittest discover -s git_viewspace_fs/tests -p '*_test.py'
