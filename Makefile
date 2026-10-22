test:
	ruff check
	mypy
	pytest

clean:
	find -regex '.*\.pyc' -exec rm {} \;
	find -regex '.*~' -exec rm {} \;
	rm -rf .mypy_cache .pytest_cache .ruff_cache

.PHONY:	test clean
