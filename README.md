# Explorador IPTV Xtream Codes

Aplicación local para sincronizar y consultar metadatos de un catálogo autorizado.
No reproduce ni descarga contenido ni genera enlaces de reproducción. Los JSON originales
pueden contener URLs u otros datos privados del proveedor: protege la base de datos
y los CSV, y no los publiques.

## Instalación

Requiere Python **3.12 o superior**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

SQLite viene incluido en Python. Se usan requests, pandas y Flask.

## Configuración

### Credenciales de TMDB

El archivo privado `tmdb.local.json` permite guardar la clave de API en
`api_key` y el token de acceso de lectura en `read_access_token`. Pega los
valores entre las comillas vacías; no añadas el prefijo `Bearer` al token.
No es necesario guardar la contraseña de tu cuenta de TMDB.

Al abrir una ficha de película o serie, la sección «Información de TMDB»
consulta automáticamente usando estas credenciales. Solo envía títulos
(sin etiquetas finales de calidad ni año entre paréntesis) y año a
`api.themoviedb.org`; no envía credenciales IPTV, URLs de reproducción
ni el JSON original. Se solicitan metadatos en español y plataformas de
Canadá (`CA`), con atribución a TMDB y JustWatch.

Los resultados se guardan en la tabla independiente `tmdb_metadata`,
incluyendo fecha UTC, país e identidad del título. Al volver a la ficha se
reutilizan sin llamadas externas. «Actualizar TMDB» fuerza otra consulta;
la disponibilidad puede cambiar desde la fecha guardada. Los resultados
vacíos y los candidatos también se guardan. Cambiar el nombre o el año
del registro invalida su asociación. Los errores se muestran explícitamente
y no sustituyen resultados guardados ni alteran el catálogo del proveedor.

Solo se asigna automáticamente una coincidencia de título normalizado
(traducido u original) y año exactos, sin otra coincidencia exacta rival.
Si falta el año o hay dudas, se muestran candidatos para elegir manualmente.
La ficha original aparece de inmediato; la consulta complementaria requiere
JavaScript. Listados, exportaciones y sincronización no consultan TMDB.

La sección añade sinopsis, géneros, reparto con personajes, dirección,
idioma original, países, productoras, IMDb, valoración con número de votos,
temporadas/episodios en series, tráiler e imagen de fondo cuando estén
disponibles. Las discrepancias con la fuente original se presentan separadas,
sin sobrescribirlas. Los idiomas no certifican los audios de la copia IPTV.
La imagen de fondo original se oculta solo cuando la de TMDB carga
correctamente, para no mostrar ambas. Si TMDB no tiene imagen o esta falla,
se conserva la original; no se elimina de la base de datos.

Las plataformas se muestran con logos locales de TMDB, separados por
suscripción, alquiler, compra, gratis y anuncios. El nombre completo aparece
al pasar el cursor o enfocar el icono; los planes y canales no se fusionan.

### Filtro «Dónde se puede ver»

El orden web predeterminado es «Valoración · Más fiables primero». Para ordenar,
se usa `(votos × nota + 100 × media) / (votos + 100)`, donde la media es la
media de notas válidas del catálogo correspondiente (películas o series),
sin depender de los filtros activos. Solo cuentan asociaciones TMDB vigentes
con nota entre 0 y 10 y votos positivos. Las notas sin votos quedan al final.
No requiere consultas nuevas a TMDB ni modifica los datos guardados.
Las tarjetas y fichas conservan la nota real, muestran el número de votos e
indican «Pocos votos» cuando hay menos de 100.
En caso de empate en la puntuación ponderada,
año descendente (más recientes primero). Los valores ausentes quedan al final.
Puedes cambiar el orden desde los filtros.
Las notas de tarjetas y del ordenamiento son exclusivamente de TMDB:
coincidencia confirmada, caché vigente de Canadá, nota numérica de 0 a 10 y
al menos un voto. Sin esos datos se muestra «—», nunca la nota del proveedor.
Las valoraciones originales se conservan en SQLite y las exportaciones, pero
no se muestran en la ficha. No se hacen consultas externas al ordenar.

En los listados de películas y series (también Niños y Otros alfabetos) hay
un panel desplegable con logos seleccionables, búsqueda de plataformas y
país fijo **Canadá (CA)**. La búsqueda de logos
solo filtra las opciones visibles: no desmarca plataformas ya seleccionadas.
Varias plataformas se combinan con **O**, y con el resto de filtros con **Y**.
Por defecto se consulta **Suscripción**; también puedes elegir alquiler,
compra, gratis, anuncios o cualquier modalidad.

- Tus servicios se definen en `services.json`, junto a `app.py`: **Netflix** y
  **Amazon Prime Video**. Las variantes se agrupan por marca: Netflix incluye
  Kids y el plan con anuncios; Prime incluye sus variantes y Amazon Video.
  Apple TV agrupa tienda y suscripción; la modalidad elegida sigue aplicándose.
  Los canales adicionales se agrupan con su marca, no con la tienda que los vende:
  Paramount+ Amazon Channel pertenece a Paramount Plus, no a Prime.
  Esta agrupación sirve para explorar marcas, no para garantizar que tu plan
  concreto incluya cada título.
  La lista es compartida por los navegadores de esta aplicación local. Si algún
  día cambia, edita la lista `services` del archivo; se lee en cada consulta,
  sin reiniciar Flask. No hay botones para modificarla desde la interfaz ni se
  usa la antigua preferencia de SQLite.
- **Solo mis servicios** restringe la búsqueda a esa lista. Si además has marcado
  logos, se usa su intersección con tus servicios. Para explorar **otra plataforma**,
  selecciona «Todas las plataformas» y marca su logo; esto no cambia `services.json`.
  Pulsa de nuevo un logo para desmarcarlo. **Limpiar** reinicia los filtros,
  sin cambiar tus servicios. Una lista vacía en el archivo no devuelve títulos
  con «Solo mis servicios». Los errores de lectura/formato se muestran
  explícitamente, sin sustituir silenciosamente tus servicios por otra lista.
  Con «Solo mis servicios» activo, cada tarjeta muestra logos de 24 px de los
  servicios que coinciden con el filtro y la modalidad elegida. Si coincide con
  ambos, muestra ambos logos. Se usan datos e iconos locales, sin consultas a
  TMDB; un icono ausente se sustituye por el nombre de la plataforma.
- **Fuera de mis servicios** muestra solo títulos con coincidencia TMDB
  confirmada y vigente para Canadá que no aparecen en tus servicios para la
  modalidad seleccionada. Excluye pendientes, candidatos, cachés obsoletas y
  títulos sin coincidencia. Se combina con los logos seleccionados y los demás
  filtros; si un título está en Netflix y otra plataforma, queda excluido.
  Una coincidencia confirmada sin plataformas registradas también puede aparecer:
  la ausencia en estos datos no garantiza ausencia real.
- Los logos se muestran agrupados también en las fichas, con Netflix y Prime
  primero, después Apple TV, Disney Plus, HBO Max, Paramount Plus, Crave y otras
  marcas principales; el resto va en orden alfabético. No hace falta volver a
  consultar TMDB para agrupar los datos ya guardados.

El filtro usa exclusivamente los resultados TMDB locales de Canadá, con una
coincidencia confirmada y la identidad del título todavía válida. Un título
sin consultar, sin coincidencia o con candidatos ambiguos no demuestra ausencia
en una plataforma: simplemente queda fuera de los resultados al filtrar.
Con «Todas las plataformas» y sin seleccionar logos, se ve todo el catálogo
según los demás filtros. La cobertura muestra consultados, asociados,
candidatos, sin coincidencia y pendientes de **todo el catálogo de ese tipo**,
no solo la página o selección actual. La disponibilidad guardada no se actualiza
sola; puede cambiar y se puede refrescar desde la ficha.

Las fichas de películas y series muestran únicamente la tabla TMDB, no la
tabla de metadatos del proveedor. El JSON original se conserva en SQLite.
Los géneros TMDB son enlaces al catálogo local filtrado por ese género.
La valoración destaca al principio de la información TMDB mediante un anillo
porcentual, la nota sobre 10 y el número de votos. Sin votos o nota válida se
indica que no hay valoración disponible, sin presentar un cero ficticio.
En tablet y móvil, la ficha TMDB muestra los campos breves con etiqueta a la
izquierda y valor a la derecha. Sinopsis, productoras y reparto usan todo el
ancho debajo de su etiqueta.
La portada y el título forman una cabecera compacta; el contenido posterior
ocupa todo el ancho, sin mantener una columna vacía debajo de la portada.
La disponibilidad en Canadá solo muestra modalidades con plataformas conocidas,
en grupos compactos con iconos. Si no hay ninguna, muestra un único aviso.

El reproductor de YouTube se carga directamente dentro de la ficha, sin
reproducción automática: basta pulsar el botón de reproducción del propio
visor. Esto conecta con YouTube al cargar la información TMDB.
Se conserva un enlace externo por si el autor no permite
reproducción integrada. El reproductor puede mostrar publicidad.
Al consultar la ficha TMDB se prioriza español latinoamericano, después otro
español, inglés y finalmente el idioma original. Dentro de cada grupo se
prefieren vídeos oficiales. La región del vídeo en TMDB es una aproximación,
no una garantía del doblaje. Si la respuesta inicial no incluye un tráiler
latinoamericano, se consulta el listado sin filtro de idioma para comparar
las alternativas. Las fichas ya guardadas no se vuelven a
consultar automáticamente: si no tenían tráiler, pulsa «Actualizar TMDB» para
probar la nueva selección. No se extraen vídeos de IMDb.

Al final de la ficha aparece «También te podrían gustar», con hasta seis
recomendaciones de TMDB que existen en el catálogo local del mismo tipo.
Se conserva el orden recomendado por TMDB, sin duplicar versiones del mismo
título ni mostrar la obra actual o películas marcadas «No me interesa».
El cruce usa asociaciones TMDB confirmadas y vigentes; mejora conforme se
enriquece el catálogo. Si no hay coincidencias locales, no se muestra la sección.
La primera visita consulta la primera página de recomendaciones de TMDB y guarda
sus identificadores durante siete días en SQLite, incluso si la lista está vacía.
El cruce local se recalcula en cada visita para respetar nuevas asociaciones y
bloqueos, sin otra petición externa mientras la caché siga vigente.
Las asociaciones bloqueadas se obtienen una sola vez por consulta, evitando
volver a recorrer los metadatos por cada recomendación candidata.
La carga es independiente de la ficha; los errores se muestran con una opción
de reintento y no se guardan como una respuesta vacía.

El icono de marcador «Quiero ver» guarda películas y series en **Mi lista**,
accesible desde la navegación con pestañas para ambos catálogos. Está disponible
en tarjetas, fichas y recomendaciones. Al guardarlo se rellena el marcador;
otro toque lo quita. No cambia los estados de vista o «No me interesa».
La lista se guarda localmente en SQLite y se conserva durante las
sincronizaciones. La búsqueda y los filtros también funcionan dentro de la
lista; los títulos bloqueados siguen ocultos. No requiere llamadas a TMDB.

SQLite usa WAL para que las lecturas del catálogo no bloqueen el guardado de
preferencias, consultas TMDB o recomendaciones. Se activa al iniciar la
aplicación, también para bases existentes, sin borrar datos. Para respaldar una
base en uso se debe usar la API de backup de SQLite (como hace el actualizador),
no copiar solo el archivo principal: puede haber cambios en archivos `-wal`.

En Administración, «Ver películas bloqueadas» abre el catálogo de películas
marcadas «No me interesa», con controles para desbloquearlas. No se añade un
selector de bloqueadas al panel de filtros.

El buscador principal aparece sobre los filtros, siempre accesible aunque estén
plegados. Un único texto busca por título del proveedor, título traducido u
original TMDB, reparto, dirección y creadores (coincidencia en cualquiera).
Se actualiza al escribir y se combina con los filtros seleccionados.
No usa IA ni hace llamadas a TMDB; las personas solo se encuentran si su
información ya está guardada. Las nuevas consultas TMDB se incorporan sin
reconstruir índices. La API conserva `actor` y `director` para enlaces antiguos.

Los filtros de actor y director y los enlaces por persona usan exclusivamente el
reparto/dirección de una coincidencia TMDB confirmada y vigente de Canadá.
Si falta reparto o dirección TMDB, no se usan datos del proveedor como respaldo.
Los nombres de reparto, dirección y creadores en TMDB son enlaces al catálogo
local de películas y series; los personajes no se usan como nombres de actores.
La búsqueda escrita permite partes del nombre; los enlaces buscan el nombre
completo. Los creadores también aparecen en los resultados por persona, pero no
se confunden con directores. No se consulta TMDB al filtrar:
solo se pueden encontrar las personas ya guardadas en la caché local.

#### Películas vistas

El filtro **Época** de películas permite combinar **Recientes**
(2015 en adelante), **Modernas** (2000–2014), **Nostalgia** (1980–1999),
**Clásicas** (antes de 1980) y **Sin año conocido**. Sin selección incluye todas.
Las épocas se combinan con O y con los demás filtros con Y, también con
**Volver a ver**. Se aplica antes de contar y paginar.
Usa exclusivamente el estreno de la caché TMDB confirmada y vigente de Canadá;
si falta o es inválido, corresponde a «Sin año conocido».
No consulta servicios externos ni cambia las fechas originales almacenadas.

Las nuevas consultas de detalles TMDB incluyen palabras clave en la misma
petición. Si coinciden con términos explícitos de temática LGBTI, la ficha
muestra una etiqueta roja al comienzo: **Temática LGBTI · según TMDB**.
El título emergente muestra las palabras clave que justifican la etiqueta.
No se infiere por personas ni por sinopsis; la ausencia de etiqueta no descarta
esa temática. Las cachés antiguas no contienen palabras clave: usa
**Actualizar TMDB** para obtenerlas, sin actualización masiva automática.

**No me interesa**, solo en las fichas de películas, las oculta de todos los
listados y búsquedas (incluidos Niños, Otros alfabetos y resultados por persona).
Se aplica inmediatamente al pulsar el botón, sin pedir confirmación.
No hay un filtro visible de bloqueadas. Para recuperarlas, abre
`/movies?banned=only`, entra en su ficha y pulsa **Desbloquear**.
Los demás filtros siguen aplicándose en esa vista.
Una ficha enlazada directamente sigue accesible para desbloquearla.
El bloqueo se guarda por identificador del proveedor en `banned_movies`, separado
del estado de vista, los datos originales y TMDB; persiste al reiniciar y
sincronizar. No elimina películas ni hace consultas externas.

En las tarjetas y fichas de películas, **Ya la vi** guarda la marca y cambia a
**Vista**; pulsa de nuevo para marcarla como no vista. El filtro **Muéstrame**
permite mostrar todo, solo películas no vistas o solo vistas, junto con los demás
filtros. Por defecto se selecciona **Películas que no he visto**; puedes elegir
**Todo** para incluir también las vistas. Está disponible también en películas infantiles y de otros alfabetos,
pero no en series.

Las marcas se guardan en la tabla `watched_movies` de SQLite, por identificador
de película del proveedor, separadas de los datos originales y de TMDB.
Se conservan al cerrar la aplicación y sincronizar el catálogo; guarda una copia
de la base de datos para respaldarlas. No se consulta ninguna API al marcar.

#### Ampliar cobertura por lotes

En **Cobertura TMDB → Estado TMDB** puedes filtrar por **Por elegir** para abrir
las fichas con candidatos y seleccionar la obra correcta. También están
**Asociados**, **Sin coincidencia**, **Pendientes** y **Todos**. Usa únicamente
la caché vigente de Canadá, sin consultas externas. Se combina con los demás
filtros y la paginación; los filtros de plataformas requieren coincidencias
confirmadas, así que para revisar candidatos selecciona todas las plataformas
y desmarca los logos. Las películas bloqueadas siguen ocultas. Los totales de
cobertura corresponden al catálogo completo, no a la selección filtrada.

**Consultar pendientes con TMDB** es el único botón de inicio: primero actualiza
los directorios de plataformas de películas y series para Canadá y después procesa entre
1 y 500 títulos pendientes del catálogo actual (50 por defecto), sin limitarse
a los filtros o la página visibles. Los títulos se consultan secuencialmente,
con una pausa de 250 ms entre títulos; cada título puede requerir varias
peticiones. Se conserva la selección conservadora de coincidencias: nunca se
elige automáticamente un candidato ambiguo. Estos candidatos se resuelven
abriendo la ficha. Las respuestas sin coincidencia también se guardan para
evitar repetir indefinidamente la misma búsqueda.

Todas las peticiones a la API TMDB comparten además un control en el proceso:
**una sola petición en curso y al menos 250 ms entre inicios** (como máximo
4 peticiones por segundo, sin ráfagas). Abarca búsquedas, detalles y directorios,
tanto desde las fichas como desde los lotes; crear un cliente nuevo no reinicia
el límite. El intento alternativo de HTTPS con `curl` en macOS también consume
su propio turno. Las imágenes y logos del CDN no pasan por este control de API.

Ante **HTTP 429** no se reintenta la petición automáticamente: se detiene el lote
o se informa del error en la ficha, conservando los resultados previos. Se
respeta `Retry-After`, tanto en segundos como en fecha HTTP; si falta o es
inválido se aplica una pausa de **60 segundos**. Durante esa pausa todas las
nuevas consultas externas fallan inmediatamente con un mensaje del tiempo
restante, sin enviar peticiones ni bloquear esperando toda la pausa. La caché
local sigue disponible. Pasado el plazo puedes volver a intentarlo manualmente.
El endpoint de las fichas devuelve HTTP 429 con `Retry-After` durante la pausa.

Este límite es conservador y no presupone una cuota garantizada de TMDB.
El control y la pausa se comparten entre hilos, pero **no entre procesos** y se
reinician al cerrar Flask. No ejecutes varias instancias, ni el script de logos
en paralelo con un lote; tampoco reinicies la aplicación para eludir la pausa.

El proceso muestra avances, permite detenerse después de la petición/título
en curso y guarda cada respuesta por separado. Al terminar o cancelar se recarga
la página con los filtros actuales. Si TMDB falla (incluido HTTP 429), el lote
se detiene con un error visible y conserva lo ya guardado; no reintenta
indefinidamente ni oculta fallos. El siguiente lote continúa con los pendientes,
sin repetir cachés válidas ni sobrescribir elecciones manuales. Para volver a
consultar un resultado ya guardado, usa «Actualizar TMDB» en su ficha.

Solo hay un lote TMDB simultáneo por instancia del servidor local. Mantén Flask
abierto; si se cierra, el progreso visible del trabajo se pierde, pero los
resultados SQLite persisten. No hay peticiones masivas automáticas al cargar
una lista ni durante `sync`. A TMDB se envía título/año, nunca credenciales IPTV.
Los cambios usan POST con protección de sesión. Reinicia Flask una vez para
cargar esta función; no hace falta reiniciarlo después de cada lote.

### Biblioteca de logos y conexión TMDB

Las fichas ya guardadas son compatibles sin volver a consultar la película.
Si no hay logo local se conserva el nombre como etiqueta; si falla una imagen
se muestra el nombre y un aviso en lugar de dejar un icono roto.

Para descargar o actualizar el catálogo mundial de logos de plataformas de
películas y series (no solo Canadá), ejecuta:

```bash
python provider_logos.py
```

Los logos de tamaño reducido y su índice se guardan en `static/providers/`.
El comando reutiliza archivos descargados, informa de errores y requiere las
credenciales privadas de TMDB. La visualización posterior no necesita descargar
logos remotos. La lista refleja los proveedores disponibles en TMDB al ejecutar
el comando, no garantiza incluir servicios que TMDB aún no catalogue.
Se mantienen las atribuciones a TMDB y JustWatch.

La conexión valida TLS. Si `requests` no puede validar certificados en macOS,
se reintenta con `/usr/bin/curl` y su almacén de confianza del sistema,
pasando credenciales por entrada estándar, nunca en argumentos visibles.
En otros sistemas revisa los certificados o configura `REQUESTS_CA_BUNDLE`.
Los valores se guardan en texto plano y el archivo está excluido de Git.
No lo compartas ni lo incluyas en capturas. En macOS/Linux limita su lectura
con `chmod 600 tmdb.local.json`.

### Credenciales de Xtream Codes

Puedes guardar las credenciales en `config.local.json`, junto a `app.py`:

```json
{
  "server_url": "https://proveedor.example:443",
  "username": "TU_USUARIO",
  "password": "TU_CONTRASEÑA"
}
```

Completa el archivo local creado con la aplicación. Si no existe, créalo con
el formato anterior. Se lee automáticamente al ejecutar `sync`, independientemente
de la carpeta desde la que ejecutes el programa. Es JSON: usa comillas dobles;
las comillas y barras invertidas dentro de valores se escriben `\"` y `\\`.
Las credenciales se guardan **en texto plano**. El archivo está excluido de Git,
pero eso no impide que aparezca en copias de seguridad o al compartir la carpeta.
No lo publiques ni lo subas a ningún repositorio. En macOS/Linux puedes limitar
su lectura con `chmod 600 config.local.json`.

También puedes usar variables de entorno, que tienen prioridad campo a campo
sobre el archivo. No se carga `.env` automáticamente. En macOS/Linux:

```bash
export XTREAM_SERVER_URL="https://proveedor.example:443"
printf 'Usuario: '; read -r XTREAM_USERNAME
export XTREAM_USERNAME
printf 'Contraseña: '; read -rs XTREAM_PASSWORD; printf '\n'
export XTREAM_PASSWORD
```

`XTREAM_SERVER_URL` admite una ruta base y también una URL terminada en
`/player_api.php`, pero no parámetros ni credenciales embebidas. Usa HTTPS
si el proveedor lo permite: Xtream envía usuario y contraseña como parámetros.
No se siguen redirecciones para evitar reenviar las credenciales a otro destino.
No se desactiva la verificación TLS.

La base de datos predeterminada es `catalog.db`, junto a `app.py`.
Puedes cambiarla con `IPTV_DB_PATH` o con la opción global `--db`.
La exploración, las estadísticas y la exportación funcionan sin credenciales
ni conexión una vez sincronizado el catálogo.

## Consola

```bash
python app.py sync
python app.py stats
python app.py search "batman"
python app.py export movies.csv
python app.py export series.csv
python app.py --db ./datos/catalog.db stats
```

`sync` consulta `player_api.php` con usuario, contraseña y estas acciones:

| Acción | Tabla SQLite |
| --- | --- |
| `get_vod_categories` | `vod_categories` |
| `get_series_categories` | `series_categories` |
| `get_vod_streams` | `movies` |
| `get_series` | `series` |

JaimeTV solo explora películas y series: no descarga TV en vivo ni ofrece
navegación, fichas, búsqueda o estadísticas de canales. Si la base existente
contiene `live_categories` o `live_streams`, la próxima sincronización correcta
elimina esas tablas y sus exclusiones antiguas, en la misma transacción.
Abrir la aplicación no las elimina. En sincronizaciones posteriores,
`DROP TABLE IF EXISTS` comprueba su ausencia sin recorrer registros ni volver
a descargar canales. SQLite reutiliza el espacio liberado; no se ejecuta
`VACUUM` automáticamente. Películas, series y caché TMDB se conservan.

Cada registro conserva **todos** los campos de la API en `raw_json`.
Las categorías normalizan ID, nombre y padre. Los contenidos normalizan ID,
nombre, categoría, imagen, valoración, año, alta, extensión, género, fecha de
estreno y última modificación cuando existen. Los IDs se guardan como texto
para aceptar los formatos numéricos y textuales de distintos proveedores.
Los campos ausentes quedan vacíos o nulos; el JSON mantiene los valores originales.

También puedes pulsar **Sincronizar catálogo** en la barra superior de la
interfaz web. El trabajo se ejecuta en segundo plano usando las mismas
credenciales y reglas que el comando `sync`. Puedes seguir navegando:
la interfaz consulta el estado cada dos segundos y, al terminar, recarga
automáticamente la página actual conservando su URL y filtros. No es necesario
reiniciar Flask: cada consulta lee los datos actualizados de SQLite.
El resumen final queda visible durante la ejecución actual del servidor;
los errores se muestran sin recargar la página y permiten reintentar.
Solo se permite una sincronización web a la vez por instancia del servidor.
No ejecutes simultáneamente el comando de terminal. Mantén el servidor abierto
hasta terminar; no se reanuda un trabajo interrumpido al cerrar la aplicación.
Este control está pensado para el servidor local de `serve`, no para varios
procesos de servidor compartiendo una base de datos.

La sincronización descarga las seis colecciones y las combina en una
única transacción: añade IDs nuevos, actualiza los existentes solo si cambió
su JSON y conserva registros ausentes de la descarga. No escribe otra vez
los registros idénticos (aunque cambie el orden de las claves). Registra
la fecha UTC en `sync_metadata`. Un error de conexión o de formato conserva el
catálogo anterior. Si el proveedor repite un ID, se conserva el último registro y
`sync` muestra un aviso con el número de repetidos. Una lista vacía válida
**no vacía** su tabla. El resumen distingue nuevos, actualizados, sin cambios
y excluidos. No se fusionan campos de versiones diferentes: un registro
cambiado conserva el JSON completo de la última versión del proveedor.

Se excluyen categorías con palabras `ADULTO`, `ADULTOS`, `ADULT`, `XXX`,
`TERROR` o `HORROR` (sin distinguir mayúsculas ni acentos), así como sus
contenidos por categoría principal o secundaria. También se retiran registros
locales que pertenezcan a esas categorías si aún existen: es la única
eliminación automática, incluso si el proveedor cambia la categoría de un ID.
Los IDs de exclusión se obtienen de las categorías descargadas y las locales,
no están fijados a los números de un proveedor. Se recuerdan en
`sync_excluded_categories` para no reintroducir contenido si en una descarga
posterior falta la definición de la categoría bloqueada.

La tabla independiente `tmdb_metadata` no se borra ni modifica durante `sync`.
Esto también se aplica al botón «Sincronizar catálogo»: conserva las coincidencias,
candidatos, consultas sin coincidencia, fechas y disponibilidad TMDB, además de
plataformas, vistas y bloqueadas. Los cambios de nota, categoría o portada del
proveedor no invalidan TMDB. Si cambia la identidad del título (nombre, título,
año o fecha de estreno del proveedor), su consulta anterior se conserva, pero
deja de usarse para evitar asociar información a una obra distinta y vuelve
a aparecer como pendiente.
Los enriquecimientos siguen disponibles al reabrir las fichas. Si cambia el
título o año que identifica una obra, TMDB requiere una nueva asociación para
evitar mostrar metadatos de otra película; el resultado anterior no se destruye.
Listados, estadísticas y exportaciones incluyen los títulos locales conservados,
aunque ya no aparezcan en el proveedor; su disponibilidad de reproducción no
se comprueba. La API utilizada no ofrece aquí una descarga incremental real:
se consultan las seis colecciones completas y solo se aplican los cambios locales.
Hay dos reintentos ante errores transitorios y tiempos de espera de 10 segundos
para conexión y 120 para lectura por petición. La descarga completa reside en
memoria antes de la escritura; dimensiona la memoria para tu catálogo.

Si aparece `HTTP 503`, el proveedor o un intermediario responde «servicio no
disponible». Espera y vuelve a ejecutar `sync`; comprueba también si el acceso
funciona en tu reproductor habitual y que la URL y el puerto sean los indicados
por el proveedor. Si persiste, consulta al proveedor. El error no demuestra que
las credenciales sean incorrectas y no se soluciona desactivando TLS.
Tras agotar los reintentos se muestra el estado HTTP real, sin exponer credenciales.

Las estadísticas muestran los totales y las diez categorías más grandes,
incluyendo contenidos sin categoría conocida. La búsqueda consulta nombres
sin distinguir mayúsculas Unicode y trata `%` y `_` como texto literal.

Los CSV usan UTF-8 con BOM y no incluyen índice de pandas.
Exportan todos los campos originales mediante `pandas.json_normalize`
(objetos anidados a columnas con puntos; listas como valores de celda).
El nombre de destino determina el tipo: `movies.csv` o `series.csv`, también en
una carpeta existente. Un archivo existente se sobrescribe. Los CSV son datos
del proveedor sin sanitizar: al abrirlos en una hoja de cálculo, importa las
celdas como texto para evitar interpretar posibles fórmulas.

## Interfaz web

```bash
python app.py serve
# Otro puerto:
python app.py serve --port 5050
```

Abre <http://127.0.0.1:5000/> para entrar directamente en Películas.

La barra superior incluye un botón con icono para entrar o salir de pantalla
completa. También puedes salir con Esc. Se muestra en navegadores compatibles.

En tablets y móviles (menos de 1200 px), los filtros se pliegan inicialmente:
toca «Filtros» para abrirlos o cerrarlos. En tablets de 600–899 px se muestran
cuatro tarjetas por fila; de 900–1199 px, cinco. En escritorio se conserva
el panel lateral de filtros abierto.
En tablet, el encabezado mantiene los controles de tema y pantalla completa
en una sola fila; los enlaces se deslizan horizontalmente si no caben.
En móviles de menos de 600 px se muestran dos tarjetas por fila, navegación
horizontal deslizable y paginación reducida a anterior, página actual y siguiente,
con controles táctiles de al menos 44 px.

Este comando usa `catalog.db` junto a `app.py` de forma predeterminada
(salvo que hayas configurado `IPTV_DB_PATH` o `--db`). Recarga automáticamente
el servidor cuando guardas cambios en Python, sin activar el depurador.
Actualiza el navegador para ver los cambios. Para desactivar la recarga,
ejecuta `python app.py serve --no-reload`.
Esto solo afecta al servidor local; Oracle sigue ejecutándose con Gunicorn.

Administración muestra las últimas fechas registradas en UTC para aplicación,
sincronización IPTV, plataformas de Canadá y consultas TMDB (por catálogo).
Las fechas persisten entre reinicios; las operaciones antiguas sin fecha
registrada muestran «Sin registro de actualización». Un intento fallido no
reemplaza la última comprobación correcta de la aplicación.
El resumen «Cobertura TMDB» solo aparece en Administración; los listados
conservan el filtro por estado TMDB, pero no muestran los contadores de cobertura.
La cobertura muestra simultáneamente películas y series en filas separadas,
con total, consultados, asociados, por elegir, sin coincidencia y pendientes.
El número «Por elegir» enlaza al listado de candidatos de ese catálogo,
incluyendo títulos vistos e infantiles, sin filtros de plataformas.

- La sección Resumen se ha eliminado. La ruta antigua `/dashboard` redirige a
  `/movies` para conservar marcadores anteriores. Los totales siguen disponibles
  desde el comando `stats`.
- `/movies`, `/series`: búsqueda por nombre, géneros TMDB y orden A–Z/Z–A.
  La única vista es Tarjetas, con 50 por página, portadas, título, año,
  géneros y valoración.
  La paginación se aplica en SQLite, después de filtrar y ordenar todo el catálogo.
  Cambiar filtros u orden vuelve a la primera página.
  «Ordenar por» combina criterio y dirección en un único selector (por ejemplo,
  Nombre A–Z, Año más recientes primero o Valoración mayor a menor).
  Los enlaces antiguos con `view=table` se muestran en tarjetas y la antigua
  cookie de vista se elimina automáticamente.
  Los filtros se apilan en un panel lateral izquierdo en escritorio y encima
  de los resultados en móvil.
  Los géneros se eligen con etiquetas de selección múltiple:
  cualquiera de las etiquetas del mismo grupo (O), combinando los grupos
  y los demás filtros (Y). Sin selección no se restringe ese grupo.
  Los géneros de películas y series proceden de la caché TMDB confirmada y
  vigente, sin distinguir mayúsculas; filtrar no requiere consultas externas. La selección se
  conserva al ordenar, paginar y volver de una ficha. En la API se repiten
  los parámetros `category` y `genre` para seleccionar varios valores.
  También funciona en Niños, Otros alfabetos y resultados por persona
  (con páginas independientes para películas y series).
  Las fichas conservan los filtros y página de origen para volver con «Atrás».
  Las portadas se cargan directamente sin Referer y con carga diferida;
  si no hay imagen o falla, se muestra un aviso.
- «Niños» tiene listados propios en `/kids/movies` y `/kids/series`,
  con los mismos filtros y portadas.
  La selección infantil utiliza:
  películas y series con géneros TMDB Familia o Animación
  (también Family o Animation).
  Se incluyen todos los alfabetos. Se han quitado los textos explicativos sobre
  categorías y alfabetos de la interfaz, manteniendo los estados, errores y
  atribuciones de los servicios.
  En los listados principales (también Otros alfabetos), «Mostrar contenido
  infantil» permite incluirlo o excluirlo; está desactivado inicialmente.
  Cuando está desactivado, las categorías infantiles se ocultan del filtro
  y se desmarcan para evitar selecciones ocultas con cero resultados.
  Al activarlo vuelven a estar disponibles; la sección Niños no cambia.
  La preferencia se recuerda en una cookie local durante un año y se comparte
  entre Películas y Series. No se borran registros ni se requiere
  sincronizar. Estadísticas, exportaciones, CLI y resultados por persona
  siguen incluyendo todo el catálogo. `children=only|all|exclude` permite
  aplicar el filtro también en la API.
- Películas y series usan géneros TMDB, reparto, dirección, año y valoración
  de coincidencias confirmadas y vigentes. Los géneros disponibles también
  se obtienen de esa caché. Los parámetros antiguos `category` en películas y
  series se ignoran. Las tarjetas muestran el año y los géneros TMDB.
- Haz clic en el nombre de cualquier película o serie para abrir su ficha.
  Ambas muestran solo TMDB. El JSON original en SQLite no se modifica.
  El título se sitúa sobre la tabla de detalles, con «Atrás» a la derecha
  en la misma línea. La ficha muestra lateralmente la portada de `stream_icon` (o `cover` en series),
  encima de los detalles en móvil. Las series también muestran las imágenes de
  `backdrop_path` a mayor tamaño debajo de la tabla, en la columna central,
  conservando sus proporciones y sin repetir URLs. El navegador solicita cada imagen HTTP(S)
  directamente al servidor remoto, sin enviar Referer; requiere conexión.
  Si falta la URL o falla la carga, se muestra un aviso. Las demás URLs se
  presentan como texto; pueden contener datos privados. La flecha «Atrás» aparece
  a la derecha del título, en la misma línea, en todas las pantallas, sin ocupar
  una fila adicional, y vuelve a la página local
  anterior; si no hay historial local, usa el listado de origen de la ficha
  (con sus filtros) o Películas. Funciona también sin JavaScript.
- La tabla de detalle usa etiquetas legibles en español (Reparto, Dirección,
  Sinopsis, Categoría principal, etc.). La duración se identifica en minutos y,
  en series, como duración por episodio. Los nombres originales en SQLite no cambian.
- Las valoraciones se agrupan en una fila «Valoración», indicando sus escalas
  (por ejemplo, `8/10 · 4/5`). Si solo existe una, se muestra la disponible.
- El nombre aparece solo en el encabezado de la ficha. Si el título coincide
  ignorando mayúsculas, espacios y el año final entre paréntesis, no se repite.
  Un título distinto se muestra como «Título alternativo». Si falta el nombre,
  el encabezado usa el título disponible. No se modifican los datos guardados.
- Cuando `youtube_trailer` contiene un ID de vídeo de YouTube válido, la ficha
  muestra un icono de YouTube con la etiqueta accesible «Ver tráiler»,
  que abre el vídeo en otra pestaña.
  Los listados también incluyen una segunda columna «Tráiler» con un icono
  de YouTube enlazado al vídeo; queda vacía cuando no hay un ID válido.
  Se mantiene al buscar y filtrar, también en otros alfabetos y en los
  resultados por persona. No hace falta volver a sincronizar.
- Año y fecha de estreno se unen en una fila «Estreno», con fecha `DD/MM/AAAA`.
  Las fechas repetidas se muestran una sola vez y los datos originales no cambian.
- En las fichas, `added`, `modified` y `last_modified` se convierten de segundos
  Unix a `DD/MM/AAAA`, calculada en UTC pero sin mostrar hora ni zona horaria. Un valor inválido se indica explícitamente.
  Los timestamps originales se conservan en SQLite y en las exportaciones.
- Películas y series incluyen filtros de actor/actriz (`cast`) y director/directora,
  por parte del nombre y sin distinguir mayúsculas Unicode. Los filtros se combinan
  entre sí, con el título y la categoría (AND), y se conservan al ordenar
  o volver de una ficha. Usan el JSON local: no es necesario sincronizar otra vez.
  Si el proveedor no incluye reparto o dirección, ese registro no coincidirá.
- En la ficha, reparto y dirección muestran nombres sin duplicados (ignorando
  mayúsculas y espacios en los extremos). Cada nombre enlaza a `/people?name=...`,
  con todas sus películas y series locales, como actor o director, en dos listas
  de tarjetas con paginación independiente. Se compara el nombre completo sin distinguir mayúsculas para evitar
  mezclar personas con nombres parcialmente coincidentes. No se modifica el JSON
  guardado ni se necesitan nuevas llamadas a la API.
- Si la lista de categorías es idéntica a la categoría principal, la ficha muestra
  solo la fila principal, conservando su enlace. Los datos guardados no cambian.
- Las categorías de la ficha son enlaces al listado del mismo tipo de contenido,
  sin otros filtros previos. Incluyen coincidencias por categoría principal o por
  cualquiera de las secundarias de `category_ids`, sin duplicar los resultados.
- «Otros alfabetos» separa películas y series cuyo nombre contiene alguna letra
  no latina (por ejemplo, cirílico, hangul, tailandés, chino, japonés, árabe o griego).
  Los títulos en escritura latina, incluidos español, inglés, francés, portugués e
  italiano, permanecen en sus listados habituales. Acentos, cifras y símbolos no
  provocan separación; los títulos mixtos con letras no latinas sí. Es una regla
  de escritura, no una detección del idioma original: un título ruso transliterado
  en letras latinas permanece en el catálogo habitual. No se borra ni modifica nada.
  La clasificación se aplica automáticamente a los datos locales y futuras sincronizaciones.
  Las traducciones quedan pendientes; no se envía información a servicios externos.
  Ambas secciones mantienen todos sus filtros y orden en tarjetas.
  La API admite `foreign=0` (predeterminado, escritura latina) o `foreign=1`
  (otros alfabetos) en películas/series. Las estadísticas, la
  búsqueda CLI, las exportaciones y los resultados por persona incluyen ambos catálogos.
- Búsqueda instantánea con debounce de 250 ms y cancelación de peticiones antiguas.
- Selector de modo claro/oscuro con icono de sol/luna, etiqueta accesible y tooltip
  en todas las pantallas. La primera visita usa
  la preferencia del sistema; tu elección se guarda en el navegador y se aplica
  antes de cargar los estilos para evitar destellos. Si el almacenamiento está
  bloqueado, se muestra un aviso y el selector sigue funcionando en esa página.
  El cambio de tema requiere JavaScript.
- Usa «Ordenar por» (Nombre, Categoría, Año o Valoración) y «Orden» en el panel
  de filtros para ordenar todo el resultado sin perder filtros. Año y valoración
  se ordenan numéricamente y los valores ausentes quedan al final en ambos sentidos.
- Los formularios, ordenamiento y paginación funcionan también sin JavaScript.
- `/api/catalog/movies`, `/api/catalog/series`: JSON
  filtrable con `q`, `actor`, `director`, `category`, `sort_by` (`name`, `category_name`, `year`, `rating`),
  `sort` (`asc`/`desc`), `children`, `foreign` y `view` (`table`/`cards`).
  Por compatibilidad con clientes programáticos, la API conserva su respuesta
  sin paginar por defecto (`view=table`): devuelve `items` y `total`, y se ignora
  `page`. Esto no habilita una vista de tabla en la interfaz.
  La interfaz siempre solicita `view=cards`, que acepta `page` (entero positivo) y devuelve un máximo de 50
  elementos, `total`, `page`, `pages`, `per_page` y el fragmento HTML `html`.
  Las páginas superiores al límite se ajustan a la última disponible.

Bootstrap 5.3.3 se carga desde jsDelivr con comprobación de integridad. Requiere
acceso a Internet para los estilos Bootstrap; los datos y la búsqueda se sirven
localmente. Las fichas y las tarjetas
pueden cargar imágenes remotas del registro.
El servidor de desarrollo Flask escucha **solo en 127.0.0.1**, sin depurador;
no lo expongas públicamente sin HTTPS y autenticación.
La sincronización es explícita desde consola o el botón web y no hay reproducción de contenido.

## Acceso privado mediante formulario

Para publicar la web, utiliza un proxy HTTPS y un único proceso Gunicorn
(`--workers 1 --threads 4`) escuchando en `127.0.0.1:5050`.
No abras ese puerto a Internet.

La autenticación se activa con `auth.local.json` junto a la aplicación, o con
`IPTV_AUTH_PATH` apuntando a un archivo privado. Este archivo queda excluido de Git.
Debe contener `username`, `password_hash` (hash Werkzeug scrypt o PBKDF2) y
`secret_key` (secreto aleatorio de al menos 64 caracteres). No uses aquí el hash
bcrypt generado por Caddy. Genera el hash con `werkzeug.security.generate_password_hash`
y la clave con `secrets.token_hex(32)`; introduce la contraseña de forma interactiva
con `getpass`, nunca como argumento ni en el historial.
Protege el archivo con permisos `600` y conserva la clave de sesión entre reinicios.
No copies la configuración de producción al repositorio.

Cuando `IPTV_AUTH_PATH` está definido, un archivo ausente o inválido impide el
arranque. Sin archivo ni variable se conserva el uso local sin autenticación;
ese modo no debe exponerse públicamente.

`/login` muestra un formulario, sin autenticación HTTP Basic. Todas las rutas
salvo login y archivos estáticos requieren sesión; la API responde 401 en vez de
redirigir. El cierre de sesión usa POST con CSRF, igual que el acceso.
Las sesiones caducan a las 12 horas y sus cookies son Secure, HttpOnly y SameSite=Lax:
para probar el acceso usa HTTPS. Hay un límite global de 10 intentos fallidos
por minuto y proceso, compartido por los dispositivos; usa una contraseña larga
y única. Es un acceso doméstico de una sola cuenta, no un sistema multiusuario.

Al migrar desde Caddy Basic, conserva esa protección hasta instalar y probar
el formulario mediante el túnel SSH con HTTPS o un acceso HTTPS de prueba protegido.
Solo entonces elimina el bloque `basicauth`, manteniendo el proxy HTTPS.
Verifica sin cookies que `/movies` redirige a `/login`, que la API devuelve 401
y que la contraseña permite acceder. No publiques una configuración sin
`IPTV_AUTH_PATH` válido. Tras un reinicio, las páginas antiguas pueden necesitar
recargarse para renovar su token CSRF.

## Actualizar desde la propia web (Oracle)

La página **Administración** (`/admin`), accesible desde la navegación, reúne
Actualizar aplicación, Sincronizar catálogo y los controles de TMDB: actualizar
plataformas, consultar pendientes de películas o series, elegir el tamaño de lote
y detener el trabajo. Conserva las mismas protecciones de sesión y CSRF.
Los filtros y la cobertura permanecen en el catálogo; las consultas TMDB de una
ficha individual siguen disponibles en esa ficha.

El botón **Actualizar aplicación** busca `origin/main` e instala directamente,
sin pedir otra confirmación. Solo aparece con autenticación y `IPTV_UPDATE_STATE`
configurados. No hay publicación automática al hacer push.
Requiere el repositorio `/home/ubuntu/jaimetv-repo`, con su clave GitHub de solo
lectura ya configurada, y la aplicación en `/home/ubuntu/catalogo-iptv`.

Instalación inicial en Oracle (después de desplegar esta versión):

```bash
mkdir -p -m 700 /home/ubuntu/.local/state/jaimetv-update
sudo install -m 644 /home/ubuntu/jaimetv-repo/deploy/jaimetv-update.service /etc/systemd/system/jaimetv-update.service
sudo install -m 644 /home/ubuntu/jaimetv-repo/deploy/jaimetv-update.path /etc/systemd/system/jaimetv-update.path
sudo visudo -cf /home/ubuntu/jaimetv-repo/deploy/jaimetv-update.sudoers
sudo install -m 440 /home/ubuntu/jaimetv-repo/deploy/jaimetv-update.sudoers /etc/sudoers.d/jaimetv-update
sudo systemctl edit catalogo-iptv
```

En el override, conserva `IPTV_AUTH_PATH` y añade:

```ini
[Service]
Environment=IPTV_UPDATE_STATE=/home/ubuntu/.local/state/jaimetv-update
```

Después ejecuta `sudo systemctl daemon-reload` y
`sudo systemctl enable --now jaimetv-update.path`, y
`sudo systemctl restart catalogo-iptv`. La unidad path escucha un archivo de solicitud
escrito por la aplicación; el proceso web conserva `NoNewPrivileges=true` y no
ejecuta sudo. El servicio de actualización solo se inicia mediante la solicitud.

El trabajo se ejecuta fuera de Gunicorn para sobrevivir a su reinicio.
Primero descarga el código, prepara dependencias en un entorno temporal y ejecuta
las pruebas de autenticación, actualización, sincronización y estados personales.
Si fallan, no cambia la aplicación instalada. La suite completa sigue disponible
para desarrollo; no se usa como puerta de despliegue mientras haya aserciones
antiguas de interfaz pendientes de actualizar.
No admite cambios locales en el repositorio de despliegue ni archivos privados
versionados. Conserva `services.json`, configuraciones locales y base de datos.
Si hay sincronización o lote TMDB activo, rechaza la petición con un mensaje
para reintentarlo después. Durante la instalación bloquea otras acciones web.

Antes de instalar detiene el servicio y guarda código reemplazado, snapshot SQLite,
configuraciones y versiones de dependencias en subcarpetas `backup-*` del directorio
privado de estado. Instala las dependencias, reinicia y comprueba `/login`.
Si la activación falla, intenta restaurar código, datos y versiones anteriores;
un fallo de restauración también se comunica como error y requiere intervención.
Las copias se conservan: controla periódicamente el espacio libre y realiza además
copias externas. Tras la primera actualización se conserva un manifiesto para
retirar archivos que desaparezcan de Git en versiones posteriores. Los archivos
antiguos anteriores a esa primera actualización no se eliminan sin inventario.
Las migraciones especiales requieren despliegue manual; no sustituye un sistema
de releases atómicas.

El estado persiste en `status.json`; consulta fallos con:

```bash
sudo journalctl -u jaimetv-update --no-pager -n 50
```

Tras un reinicio inesperado durante la actualización, comprueba el servicio y
el registro antes de reintentar; no borres un estado `running` sin verificar
que `jaimetv-update` está inactivo. Las actualizaciones pueden interrumpir una
consulta individual; no pulses el botón durante operaciones que quieras conservar.

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Las pruebas usan SQLite temporal y una API Xtream simulada en localhost.
No requieren credenciales reales ni acceso al proveedor.
