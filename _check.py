import py_compile
py_compile.compile('strategy.py', doraise=True)
py_compile.compile('batch_strategy.py', doraise=True)
print('OK')
