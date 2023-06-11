# prometheus-s3-exporter Docker Container [![Docker Build](https://github.com/Smals-Webtech/prometheus-s3-exporter/actions/workflows/docker-build.yml/badge.svg?branch=master)](https://github.com/Smals-Webtech/prometheus-s3-exporter/actions/workflows/docker-build.yml)

# Build

To automate the build and testing of this image, we rely on a Makefile that facilitates the construction and testing of a container image for S3 Exporter.  The Makefile supports both Docker and Podman with Buildah as options for building and testing the image.  Additionally, the Dockerfile used for image creation is templated using m4.  

## Prerequisites

To use this Makefile, you need to have the following installed on your system:

- [Docker](https://docs.docker.com/get-docker/) or [Podman](https://podman.io/getting-started/installation) with [Buildah](https://buildah.io/install) (for building and managing containers)
- [Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git) (for version control)
- [Make](https://www.gnu.org/software/make/) (for running the Makefile commands)
- [M4](https://www.gnu.org/software/m4/) (for generating the Dockerfile from templates)

Make sure to follow the links provided to install the required tools according to your operating system and platform.

## Getting Started

1. Clone the repository containing the Makefile and navigate to its directory:

   ```bash
   git clone <repository_url>
   cd <repository_directory>
   ```

2. (Optional) If you want to customize the build process, create a `.build.env` file in the repository directory. This file can define the following environment variables:

   - `EXPORTER_VERSION`: The version of S3 Exporter to build (default: 3.0.2)
   - `DOCKER_IMAGE_NAME`: The name of the Docker image to build (default: docker.io/smalswebtech/prometheus-s3-exporter)

   Make sure to define these variables in the `.build.env` file using the `KEY=VALUE` format.

## Usage

To use the Makefile, you can run the following commands:

- `make build`: Build the Docker image of S3 Exporter.
- `make test`: Test the Docker image of S3 Exporter.
- `make Dockerfile`: Generate the Dockerfile from the provided templates.

You can also run `make help` to see a list of available commands.

**Note:** By default, the Makefile uses Docker as the container engine. If you want to use Podman with Buildah instead, you have two options:

1. Set the `CONTAINER_ENGINE` variable in the `.build.env` file. Create a `.build.env` file in the repository directory and define `CONTAINER_ENGINE=podman` in the file.
2. Set the `CONTAINER_ENGINE` environment variable directly when running the Makefile commands:

   ```bash
   make build CONTAINER_ENGINE=podman
   ```

Using an environment variable allows you to dynamically switch between Docker and Podman with Buildah without modifying the `.build.env` file.

Additionally, if you are using Podman as the container engine, you can specify the `CONTAINER_TARGET_IMAGE_FORMAT` environment variable to choose the image format. By default, the image format is Docker. To create the image in the OCI format, use the following command:

   ```bash
   make build CONTAINER_ENGINE=podman CONTAINER_TARGET_IMAGE_FORMAT=oci
   ```

To customize the Docker image name and S3 Exporter version, you have two options:

1. Set the `DOCKER_IMAGE_NAME` and `EXPORTER_VERSION` variables in the `.build.env` file. Create a `.build.env` file in the repository directory and define the desired values for these variables.
2. Set the `DOCKER_IMAGE_NAME` and `EXPORTER_VERSION` environment variables directly when running the Makefile commands:

   ```bash
   make build DOCKER_IMAGE_NAME=my-custom-image EXPORTER_VERSION=6.0.0
   ```

Setting these variables allows you to customize the image name and S3 Exporter version without modifying the `.build.env` file.

Please ensure that you have the necessary dependencies installed as mentioned earlier in the documentation.

## Customizing the Build

If you want to customize the build process further, you can modify the `.build.env` file to set the desired values for the environment variables mentioned earlier. Additionally, you can modify the Dockerfile templates located in the `Dockerfiles` directory. The Makefile uses `m4` to generate the final Dockerfile from the templates.

To regenerate the Dockerfile after modifying the templates, run the following command:

```bash
make Dockerfile
```

## Testing

The Makefile uses Bats (Bash Automated Testing System) to test the Docker images. The test cases are defined in the `test/tests.bats` file. Before running the tests, make sure you have the following dependencies installed:

- Bats: Bats is a TAP-compliant testing framework for Bash. Install Bats by following the instructions in the [Bats documentation](https://github.com/bats-core/bats-core#installation).  
- gettext: gettext is a package that provides internationalization (i18n) support. Install gettext by following the instructions for your specific operating system.  
- Docker: If you're using Docker as the container engine, you need to have Docker installed. Follow the instructions in the [Docker documentation](https://docs.docker.com/get-docker/) to install Docker.  
- Docker Compose: Docker Compose is required for certain tests that use Docker Compose functionality. Install Docker Compose by following the instructions in the [Docker Compose documentation](https://docs.docker.com/compose/install/).  
- Podman (with Podman Compose): If you're using Podman as the container engine, you need to have Podman and Podman Compose installed. Install Podman by following the instructions in the [Podman documentation](https://podman.io/getting-started/installation). Install Podman Compose by following the instructions in the [Podman Compose documentation](https://github.com/containers/podman-compose#installation).  

To run the tests, make sure to configure the desired container engine (Docker or Podman) using the CONTAINER_ENGINE environment variable. The Makefile will execute the tests accordingly.

To run the tests, use the following commands:

- `make test`: Test the Docker image of S3 Exporter using the configured container engine.

You can also specify the `DOCKER_IMAGE_NAME` and `EXPORTER_VERSION` variables to customize the image name and version used for testing. For example:

```shell
make test DOCKER_IMAGE_NAME=my-custom-image EXPORTER_VERSION=6.0.0 CONTAINER_ENGINE=podman
```

The Bats test suite includes multiple test cases that validate the functionality and behavior of the S3 Exporter container image. It covers various aspects of the image, including its configuration, dependencies, and expected output. The test suite ensures the integrity and correctness of the container image.  

# Releases

Releases are done via GitHub actions and uploaded on Docker Hub.

# Supported tags and respective Dockerfile links

- [`x.y.z`, `x.y`, `x`, `x.y.z-prd`, `x.y-prd`, `x-prd`, `latest`](Dockerfiles/Dockerfile.in)
