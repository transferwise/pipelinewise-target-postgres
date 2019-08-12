#!/usr/bin/env python

from setuptools import setup

with open('README.md') as f:
      long_description = f.read()

setup(name="pipelinewise-target-postgres",
      version="1.0.1",
      description="Singer.io target for loading data to PostgreSQL - PipelineWise compatible",
      long_description=long_description,
      long_description_content_type='text/markdown',
      author="TransferWise",
      url='https://github.com/transferwise/pipelinewise-target-postgres',
      classifiers=[
          'License :: OSI Approved :: MIT License',
          'Programming Language :: Python :: 3 :: Only'
      ],
      py_modules=["target_postgres"],
      install_requires=[
          'singer-python==5.1.1',
          'psycopg2==2.8.2',
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
