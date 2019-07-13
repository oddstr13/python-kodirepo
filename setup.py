from setuptools import setup, find_packages

setup(
    name='kodirepo',
    version='2.3.0',
    packages=find_packages(),
    install_requires=[
        'GitPython',
        'click',
        'click-log',
        'semantic_version',
    ],
    entry_points='''
        [console_scripts]
        kodirepo=kodirepo.__main__:cli
    ''',
    zip_safe=True,
)
