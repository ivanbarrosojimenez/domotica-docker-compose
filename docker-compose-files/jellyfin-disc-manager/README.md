# Jellyfin Disc Manager — fase de diagnóstico segura

Este auxiliar sigue las variables del Docker Swarm existente y comienza **SIN realizar borrados, SIN ejecutar Media Cleaner y SIN crear placeholders**.

## Material

- `docker-compose.yml` — Stack de Portainer/GitHub
- `disc_manager.py` — programa Python, sin dependencias externas

## Por qué no se activa directamente

Para evitar crear placeholders por cambios de nombre, pérdidas de conexión del NAS o eliminaciones externas, la creación real solo se habilita cuando `disc_manager.py` toma un inventario de elementos vistos, ejecuta **él mismo** la tarea de Media Cleaner y comprueba que terminó correctamente. Las opciones de escritura permanecen deshabilitadas por defecto. El estado «Visto» de los placeholders y su protección contra Media Cleaner también deben probarse antes del uso diario.

## Instalación inicial — solo lectura

1. En `docker-worker-2`, comprobar que `/mnt/nas/Peliculas` y `/mnt/nas/Series` están montados y son accesibles.
2. Crear los marcadores de seguridad en la raíz de **ambas** bibliotecas (solo una vez):
   ```bash
   sudo touch /mnt/nas/Peliculas/.jellyfin-disc-manager-ready /mnt/nas/Series/.jellyfin-disc-manager-ready
   ```
3. Crear la carpeta del auxiliar en el **worker**:
   ```bash
   sudo install -d -o 1000 -g 1000 -m 750 /home/ivan/docker/jellyfin-disc-manager
   ```
4. Copiar `disc_manager.py` dentro de esa carpeta. **El Stack enlazado con Git no copia automáticamente este archivo al host del worker.** La copia debe hacerse con `scp`, `curl` desde tu propio GitHub o un sistema de despliegue existente. Dar permisos de lectura:
   ```bash
   sudo chown 1000:1000 /home/ivan/docker/jellyfin-disc-manager/disc_manager.py
   sudo chmod 640 /home/ivan/docker/jellyfin-disc-manager/disc_manager.py
   ```
5. En Portainer, crear un Stack Git con `docker-compose.yml` y establecer estas variables privadas en Portainer, nunca en el repositorio público:
   - `NODE_HOSTNAME=docker-worker-2`
   - `DOCKER_PATH_PERSISTENT=/home/ivan/docker` (confirmar en worker)
   - `DOCKER_PATH_MEDIA=/mnt/nas`
   - `DOCKER_TZ=Europe/Madrid`
   - `JELLYFIN_USER_ID=<id del usuario cuyo visto debe conservarse>`
   - `JELLYFIN_API_KEY=<clave creada para este auxiliar>`
6. Desplegar. En modo seguro no es necesario dar acceso de escritura a los directorios multimedia.

## Pruebas

Desde un manager:

```bash
docker service ps NOMBRESTACK_jellyfin-disc-manager
docker service logs --tail 50 NOMBRESTACK_jellyfin-disc-manager
```

Desde el **worker**, entrar en el contenedor y crear una instantánea manual (solo lectura):

```bash
CID=$(docker ps -q --filter label=com.docker.swarm.service.name=NOMBRESTACK_jellyfin-disc-manager | head -1)
docker exec "$CID" python /app/disc_manager.py --once
```

Para comparar con el inventario tras ejecutar una limpieza manual, sin crear archivos:

```bash
docker exec "$CID" python /app/disc_manager.py --report
```

`--report` siempre es de solo lectura. La instantánea está en `/home/ivan/docker/jellyfin-disc-manager/latest-snapshot.json`.

## Activación futura (todavía NO hacer)

Para un ciclo real se necesitará cambiar de forma consciente **cuatro** elementos: `DRY_RUN=false`, `RUN_MEDIA_CLEANER=true`, permisos `:rw` para las bibliotecas, y coordinar/eliminar la programación propia de Media Cleaner para que solo se ejecute cuando el auxiliar la lance. `RESTORE_WATCHED` está en false hasta verificar el escaneo, la recuperación de estado y una regla de protección para placeholders. No habilites la limpieza automática sin esas comprobaciones: un `.disc` visto hace >30 días puede ser eliminado de nuevo por Media Cleaner.
