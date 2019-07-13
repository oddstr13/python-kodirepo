from setuptools import setup, find_packages

from kodirepo.version import version

setup(
    name='kodirepo',
    version=version,
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
