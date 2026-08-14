# Trinity Makefile (Windows compatible)
# On Windows use:  make              (if make.exe is in PATH)
#                   nmake /f Makefile (MSVC variant)
# On Linux/Mac:     make
#
# If neither make nor nmake is available, run the commands directly.

PYTHON := python
TRINITY_ROOT := C:\Users\Administrator\trinity

.PHONY: test lint clean help

help: ## Show this help
	@echo "Trinity Makefile targets:"
	@echo "  make test   - Run batch self-tests"
	@echo "  make lint   - Quick syntax check on trinity package"
	@echo "  make clean  - Remove __pycache__ directories"

test: ## Run batch self-tests
	$(PYTHON) scripts\run_all_self_tests.py --target trinity

lint: ## Quick syntax / import check
	$(PYTHON) -c "import trinity; print('trinity', trinity.__version__, 'OK')"
	$(PYTHON) -c "import compileall; compileall.compile_dir('trinity', quiet=1, force=True); print('Compile check OK')"

clean: ## Remove __pycache__ directories
	@for /r trinity %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d" 2>nul
	@echo Clean done.
