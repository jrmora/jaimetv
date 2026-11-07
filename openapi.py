def _query(name, description, schema_type="string", enum=None, array=False):
    schema = {"type": "array", "items": {"type": schema_type}} if array else {"type": schema_type}
    if enum:
        (schema["items"] if array else schema)["enum"] = enum
    parameter = {
        "name": name,
        "in": "query",
        "required": False,
        "description": description,
        "schema": schema,
    }
    if array:
        parameter.update(style="form", explode=True)
    return parameter


def _form_body(properties, required=()):
    return {
        "required": False,
        "description": "La interfaz Swagger agrega automáticamente el token CSRF de la sesión.",
        "content": {
            "application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                }
            }
        },
    }


def _responses(*codes):
    return {
        str(code): {
            "description": description,
            **({
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/JsonObject"}
                    }
                }
            } if code != 204 else {}),
        }
        for code, description in codes
    }


def build_openapi_spec(server_url="/"):
    """Return the OpenAPI contract for the application's JSON endpoints."""
    catalog_parameters = [
        {"name": "kind", "in": "path", "required": True, "schema": {"type": "string", "enum": ["movies", "series"]}},
        _query("q", "Búsqueda por título."),
        _query("category", "IDs de categoría; se puede repetir.", array=True),
        _query("genre", "Géneros; se puede repetir.", array=True),
        _query("actor", "Filtra por actor."),
        _query("director", "Filtra por director."),
        _query("person", "Filtra por persona."),
        _query("sort", "Dirección del ordenamiento.", enum=["asc", "desc"]),
        _query("sort_by", "Campo de ordenamiento.", enum=["name", "category_name", "year", "rating", "votes"]),
        _query("children", "Filtro de contenido infantil.", enum=["all", "only", "exclude"]),
        _query("foreign", "Incluir títulos de otros alfabetos (0 o 1).", enum=["0", "1"]),
        _query("page", "Número de página.", schema_type="integer"),
        _query("view", "Formato adicional de tarjetas.", enum=["table", "cards"]),
        _query("watched", "Filtro de vistos.", enum=["all", "seen", "unseen"]),
        _query("banned", "Filtro de títulos bloqueados.", enum=["hide", "only"]),
        _query("tmdb_status", "Estado de asociación TMDB.", enum=["all", "matched", "candidates", "missing", "pending"]),
        _query("provider", "Plataformas; se puede repetir.", array=True),
        _query("access", "Modalidad de disponibilidad.", enum=["all", "flatrate", "rent", "buy", "free", "ads"]),
        _query("my_services", "Filtro de servicios propios (0, 1 o 2).", enum=["0", "1", "2"]),
        _query("era", "Época de películas; se puede repetir.", array=True),
        _query("kids_category", "Categorías personales infantiles; se puede repetir.", array=True),
        _query("watchlist", "Filtrar por Mi lista (0 o 1).", enum=["0", "1"]),
    ]
    kind_parameter = {"name": "kind", "in": "path", "required": True, "schema": {"type": "string", "enum": ["movies", "series"]}}
    record_parameter = {"name": "record_id", "in": "path", "required": True, "schema": {"type": "string"}}

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "JaimeTV API",
            "version": "1.0.0",
            "description": (
                "Rutas JSON de JaimeTV. Las rutas HTML no se incluyen. "
                "Si la autenticación está habilitada, usa esta interfaz desde una sesión iniciada. "
                "Swagger agrega el token CSRF de la sesión a las operaciones POST."
            ),
        },
        "servers": [{"url": server_url}],
        "tags": [
            {"name": "Catálogo"},
            {"name": "Sincronización"},
            {"name": "TMDB"},
            {"name": "Actualización"},
        ],
        "paths": {
            "/api/catalog/{kind}": {
                "get": {
                    "tags": ["Catálogo"],
                    "summary": "Consultar películas o series",
                    "description": "Devuelve resultados y filtros del catálogo local. Los parámetros repetibles aceptan varios valores.",
                    "parameters": catalog_parameters,
                    "responses": _responses((200, "Resultados del catálogo"), (400, "Parámetros inválidos"), (401, "Sesión requerida")),
                }
            },
            "/api/sync": {
                "get": {
                    "tags": ["Sincronización"],
                    "summary": "Consultar el estado de sincronización",
                    "responses": _responses((200, "Estado actual"), (401, "Sesión requerida")),
                },
                "post": {
                    "tags": ["Sincronización"],
                    "summary": "Iniciar sincronización del catálogo",
                    "description": "Inicia un trabajo en segundo plano.",
                    "requestBody": _form_body({}),
                    "responses": _responses((202, "Trabajo iniciado"), (409, "Ya hay una sincronización en curso"), (401, "Sesión requerida")),
                },
            },
            "/api/tmdb/batch": {
                "get": {
                    "tags": ["TMDB"],
                    "summary": "Consultar el estado del lote TMDB",
                    "responses": _responses((200, "Estado actual"), (401, "Sesión requerida")),
                },
                "post": {
                    "tags": ["TMDB"],
                    "summary": "Iniciar, cancelar o preparar un lote TMDB",
                    "requestBody": _form_body({
                        "action": {"type": "string", "enum": ["start", "directory", "cancel"], "default": "start"},
                        "kind": {"type": "string", "enum": ["movies", "series"]},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    }),
                    "responses": _responses((202, "Trabajo iniciado"), (400, "Parámetros inválidos"), (409, "Ya hay un proceso en curso"), (401, "Sesión requerida")),
                },
            },
            "/api/tmdb/{kind}/{record_id}": {
                "get": {
                    "tags": ["TMDB"],
                    "summary": "Consultar metadatos TMDB de una ficha",
                    "parameters": [kind_parameter, record_parameter],
                    "responses": _responses((200, "Metadatos y fragmentos HTML"), (404, "Ficha inexistente"), (502, "Error del servicio TMDB"), (401, "Sesión requerida")),
                },
                "post": {
                    "tags": ["TMDB"],
                    "summary": "Actualizar o seleccionar la asociación TMDB",
                    "parameters": [kind_parameter, record_parameter],
                    "requestBody": _form_body({
                        "selected": {"type": "integer", "description": "ID del candidato elegido."},
                        "manual_tmdb": {"type": "string", "description": "ID o URL válida de TMDB."},
                    }),
                    "responses": _responses((200, "Metadatos actualizados"), (400, "Selección inválida"), (404, "Ficha inexistente"), (429, "Límite TMDB alcanzado"), (502, "Error del servicio TMDB"), (401, "Sesión requerida")),
                },
            },
            "/api/tmdb/{kind}/{record_id}/recommendations": {
                "get": {
                    "tags": ["TMDB"],
                    "summary": "Consultar recomendaciones TMDB",
                    "parameters": [kind_parameter, record_parameter],
                    "responses": _responses((200, "Recomendaciones"), (404, "Ficha inexistente"), (429, "Límite TMDB alcanzado"), (502, "Error del servicio TMDB"), (401, "Sesión requerida")),
                }
            },
            "/api/application-update": {
                "get": {
                    "tags": ["Actualización"],
                    "summary": "Consultar el estado de actualización de la aplicación",
                    "description": "Disponible solo cuando el servicio de actualización está configurado.",
                    "responses": _responses((200, "Estado de actualización"), (404, "Actualización no configurada"), (401, "Sesión requerida")),
                },
                "post": {
                    "tags": ["Actualización"],
                    "summary": "Solicitar actualización de la aplicación",
                    "description": "Operación administrativa. Requiere que la actualización esté configurada.",
                    "requestBody": _form_body({}),
                    "responses": _responses((202, "Actualización iniciada"), (404, "Actualización no configurada"), (409, "Hay un proceso incompatible en curso"), (503, "No se pudo iniciar"), (401, "Sesión requerida")),
                },
            },
        },
        "components": {
            "schemas": {
                "JsonObject": {
                    "type": "object",
                    "description": "Objeto JSON específico de cada operación.",
                    "additionalProperties": True,
                }
            }
        },
    }