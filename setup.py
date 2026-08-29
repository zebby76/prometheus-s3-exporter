"""Setup script for prometheus-s3-exporter package."""

import versioneer
from setuptools import find_packages, setup

setup(
    name="prometheus-s3-exporter",
    version=versioneer.get_version(),
    cmdclass=versioneer.get_cmdclass(),
    description="WebTech Monitoring - Expose S3 bucket size as a Prometheus Metric.",
    url="https://github.com/zebby76/prometheus-s3-exporter",
    author="Sebastian Molle",
    author_email="sebastian.molle@gmail.com",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "Topic :: System :: Monitoring",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    keywords="monitoring prometheus s3",
    packages=find_packages(exclude=["test", "test.*"]),
    install_requires=[
        "PyYAML==6.0.2",
        "prometheus-client==0.22.1",
        "requests==2.32.4",
        "falcon==4.0.2",
        "boto3==1.39.3",
    ],
    extras_require={"dev": ["pytest==8.3.4"]},
    entry_points={"console_scripts": ["exporter=exporter:main"]},
    python_requires=">=3.12, <3.13",
)
