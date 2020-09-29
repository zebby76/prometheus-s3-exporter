function init_clair_local_scanner_config_volume {

  local -r _volume_name=$1
  local -r _filename=$2

  local -r _copy_status=0

  if [ -f ${_filename} ]; then

    docker container create --name dummy -v $_volume_name:/configs alpine:latest

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

    run docker cp -a ${_filename} dummy:/configs/

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

    docker rm dummy

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

  fi

  if [ "$_copy_status" -eq 0 ]; then
    echo "FS-VOLUME CLAIR-LOCAL-SCANNER CONFIG COPY OK"
    return 0
  else
    echo "FS-VOLUME CLAIR-LOCAL-SCANNER CONFIG COPY KO"
    false
  fi

}

function load_database {
  local -r _container_name=${1}
  local -r _filename=${2}

  local -r _DB_DRIVER=${3}
  local -r _DB_ROOT_USER=${4}
  local -r _DB_ROOT_PASSWORD=${5}
  local -r _DB_ROOT_NAME=${6}

  local -r _DB_PORT=${7}
  local -r _DB_HOST=${8}
  local -r _DB_USER=${9}
  local -r _DB_PASSWORD=${10}
  local -r _DB_NAME=${11}

  if [ ${_DB_DRIVER} = mysql ] ; then
    run load_mysql $_container_name $_filename $_DB_ROOT_USER $_DB_ROOT_PASSWORD $_DB_ROOT_NAME $_DB_PORT $_DB_HOST $_DB_USER $_DB_PASSWORD $_DB_NAME
  elif [ ${_DB_DRIVER} = pgsql ] ; then
    run load_pgsql $_container_name $_filename $_DB_ROOT_USER $_DB_ROOT_PASSWORD $_DB_ROOT_NAME $_DB_PORT $_DB_HOST $_DB_USER $_DB_PASSWORD $_DB_NAME
  else
    echo "Driver ${_DB_DRIVER} not supported"
    return -1
  fi;

  if [ $? -eq 0 ]; then
    echo "${_DB_DRIVER} OK"
    return 0
  else
    echo "${_DB_DRIVER} KO"
    false
  fi

}

function load_mysql {
  local -r _container_name=$1
  local -r _filename=$2

  local -r _DB_ROOT_USER=${3}
  local -r _DB_ROOT_PASSWORD=${4}
  local -r _DB_ROOT_NAME=${5}

  local -r _DB_PORT=${6}
  local -r _DB_HOST=${7}
  local -r _DB_USER=${8}
  local -r _DB_PASSWORD=${9}
  local -r _DB_NAME=${10}

  local -r _basename=$(basename $_filename)
  local -r _relative=${_filename#"${BATS_TEST_DIRNAME}/"}

  local -r _name=${_basename%.*}

  local _mysql_status=0

  run docker exec ${_container_name} sh -c "mysql -u${_DB_ROOT_USER} -p${_DB_ROOT_PASSWORD} -vvv -e \"CREATE DATABASE ${_DB_NAME};\""
  run docker exec ${_container_name} sh -c "mysql -u${_DB_ROOT_USER} -p${_DB_ROOT_PASSWORD} -vvv -e \"CREATE USER '${_DB_USER}'@'%' IDENTIFIED BY '${_DB_PASSWORD}';\""
  run docker exec ${_container_name} sh -c "mysql -u${_DB_ROOT_USER} -p${_DB_ROOT_PASSWORD} -vvv -e \"GRANT ALL PRIVILEGES ON ${_DB_NAME} . * TO '${_DB_USER}'@'%';\""
  run docker exec ${_container_name} sh -c "mysql -u${_DB_ROOT_USER} -p${_DB_ROOT_PASSWORD} -vvv -e \"FLUSH PRIVILEGES;\""
  assert_output -l -r "Query OK, .* rows affected \(.*\)"

  if [ ! "$?" -eq 0 ]; then
    _mysql_status=1
  fi

  if [ -f ${BATS_TEST_DIRNAME%/}/dumps/${_name}.tar.gz ]; then
    run tar xvzf ${BATS_TEST_DIRNAME%/}/dumps/${_name}.tar.gz -C ${BATS_TEST_DIRNAME%/}/dumps

    if [ ! "$?" -eq 0 ]; then
      _mysql_status=1
    fi

    run docker cp ${BATS_TEST_DIRNAME%/}/dumps/${_name}.sql ${_container_name}:/tmp/${_name}.sql

    if [ ! "$?" -eq 0 ]; then
      _mysql_status=1
    fi

    #run docker exec ${_container_name} sh -c "mysql --port=${_DB_PORT} --host=${_DB_HOST} --user=${_DB_USER} --password=${_DB_PASSWORD} ${_DB_NAME} -vvv < /tmp/${_name}.sql"
    run docker exec ${_container_name} sh -c "mysql --port=${_DB_PORT} --host=${_DB_HOST} --user=${_DB_USER} --password=${_DB_PASSWORD} -e \"use ${_DB_NAME}; set autocommit=0; source /tmp/${_name}.sql; commit;\""
    #assert_output -l -r "Query OK, .* rows affected \(.*\)"

    if [ ! "$?" -eq 0 ]; then
      _mysql_status=1
    fi

    run rm ${BATS_TEST_DIRNAME%/}/dumps/${_name}.sql

    if [ ! "$?" -eq 0 ]; then
      _mysql_status=1
    fi

  fi

  if [ "$_mysql_status" -eq 0 ]; then
    echo "${_DB_DRIVER} OK"
    return 0
  else
    echo "${_DB_DRIVER} KO"
    false
  fi

}

function load_pgsql {
  local -r _container_name=${1}
  local -r _filename=${2}

  local -r _DB_ROOT_USER=${3}
  local -r _DB_ROOT_PASSWORD=${4}
  local -r _DB_ROOT_NAME=${5}

  local -r _DB_PORT=${6}
  local -r _DB_HOST=${7}
  local -r _DB_USER=${8}
  local -r _DB_PASSWORD=${9}
  local -r _DB_NAME=${10}

  local -r _basename=$(basename $_filename)
  local -r _relative=${_filename#"${BATS_TEST_DIRNAME}/"}

  local -r _name=${_basename%.*}

  local _pgsql_status=0

  run docker exec ${_container_name} sh -c "PGHOST=${_DB_HOST} PGPORT=${_DB_PORT} PGDATABASE=${_DB_ROOT_NAME} PGUSER=${_DB_ROOT_USER} PGPASSWORD=${_DB_ROOT_PASSWORD} psql --command=\"CREATE USER ${_DB_USER} WITH PASSWORD '${_DB_PASSWORD}';\""

  if [ ! "$?" -eq 0 ]; then
    _pgsql_status=1
  fi

  run docker exec ${_container_name} sh -c "PGHOST=${_DB_HOST} PGPORT=${_DB_PORT} PGDATABASE=${_DB_ROOT_NAME} PGUSER=${_DB_ROOT_USER} PGPASSWORD=${_DB_ROOT_PASSWORD} psql --command=\"CREATE DATABASE ${_DB_NAME} WITH OWNER ${_DB_USER};\""

  if [ ! "$?" -eq 0 ]; then
    _pgsql_status=1
  fi

  run docker exec ${_container_name} sh -c "PGHOST=${_DB_HOST} PGPORT=${_DB_PORT} PGDATABASE=${_DB_ROOT_NAME} PGUSER=${_DB_ROOT_USER} PGPASSWORD=${_DB_ROOT_PASSWORD} psql --command=\"GRANT ALL PRIVILEGES ON DATABASE ${_DB_NAME} TO ${_DB_USER};\""

  if [ ! "$?" -eq 0 ]; then
    _pgsql_status=1
  fi

  if [ -f ${BATS_TEST_DIRNAME%/}/dumps/${_name}.tar.gz ]; then
    run tar xvzf ${BATS_TEST_DIRNAME%/}/dumps/${_name}.tar.gz -C ${BATS_TEST_DIRNAME%/}/dumps

    if [ ! "$?" -eq 0 ]; then
      _pgsql_status=1
    fi

    run docker cp ${BATS_TEST_DIRNAME%/}/dumps/${_name}.dump ${_container_name}:/tmp/${_name}.dump

    if [ ! "$?" -eq 0 ]; then
      _pgsql_status=1
    fi

    #run docker exec ${_container_name} sh -c "PGPASSWORD=${_DB_PASSWORD} pg_restore -h elasticms_pgsql -p 5432 -U ${_DB_USER} -d ${_DB_NAME} /tmp/${_name}.dump"
    run docker exec ${_container_name} sh -c "PGHOST=${_DB_HOST} PGPORT=${_DB_PORT} PGDATABASE=${_DB_NAME} PGUSER=${_DB_USER} PGPASSWORD=${_DB_PASSWORD} psql < /tmp/${_name}.dump"
    
    if [ ! "$?" -eq 0 ]; then
      _pgsql_status=1
    fi

    run rm ${BATS_TEST_DIRNAME%/}/dumps/${_name}.dump

    if [ ! "$?" -eq 0 ]; then
      _pgsql_status=1
    fi

  fi

  if [ "$_pgsql_status" -eq 0 ]; then
    return 0
  else
    return -1
  fi

}

function init_s3bucket {

  local -r _filename=$1
  local -r _bucket=$2
  local -r _endpoint=$3

  local -r _basename=$(basename $_filename)
  local -r _name=${_basename%.*}
  local -r _relative=${_filename#"${BATS_TEST_DIRNAME}/"}

  local _copy_status=0

  if [ -f ${_filename} ]; then

    mkdir -p /tmp/assets 

    run tar xvzf ${_filename} --strip-components=1 -C /tmp/assets

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

    run aws s3 cp /tmp/assets/ s3://${_bucket%/}/ --endpoint-url ${_endpoint} --recursive
    assert_output -l -r ".*upload: .* to s3://${_bucket%/}/.*"

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

    run rm -rf /tmp/assets

    if [ ! "$?" -eq 0 ]; then
      _copy_status=1
    fi

  fi 

  if [ "$_copy_status" -eq 0 ]; then
    echo "S3 DATA COPY OK"
    return 0
  else
    echo "S3 DATA COPY KO"
    false
  fi

}
