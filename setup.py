from setuptools import setup, find_packages

setup(
    name='kodirepo',
    version='2.3.0',
    packages=find_packages(),
    install_requires=[
        'GitPython',
        'click',
    ],
    entry_points='''
        [console_scripts]
        kodirepo=kodirepo.__main__:main
    ''',
    zip_safe=True,
)
