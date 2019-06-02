#!/usr/bin/env python

from setuptools import setup

setup(name="pipelinewise-target-postgres",
      version="1.0.0",
      description="Singer.io target for loading data to PostgreSQL - PipelineWise compatible",
      author="TransferWise",
      url='https://github.com/transferwise/pipelinewise-target-postgres',
      classifiers=["Programming Language :: Python :: 3 :: Only"],
      py_modules=["target_postgres"],
      install_requires=[
          'singer-python==5.1.1',
          'psycopg2==2.7.5',
          'inflection==0.3.1',
          'joblib==0.13.2'
      ],
      entry_points="""
          [console_scripts]
          target-postgres=target_postgres:main
      """,
      packages=["target_postgres"],
      package_data = {},
      include_package_data=True,
)