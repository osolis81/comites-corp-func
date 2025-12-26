import base64
import urllib.parse
import re
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import azure.functions as func
from gestor_clickup import crear_tarea_madre, crear_subtareas_acuerdos


app = func.FunctionApp()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_TOKEN_CACHE = {"access_token": None, "expires_at": 0}


from datetime import date

LOCAL_UTC_OFFSET_HOURS = int(os.getenv("LOCAL_UTC_OFFSET_HOURS", "-5"))

def local_date_range_to_utc(local_start_yyyy_mm_dd: str, local_end_yyyy_mm_dd: str) -> tuple[str, str]:
    """
    Convierte un rango de fechas LOCAL (00:00 a 24:00) a UTC (ISO Z).
    endLocalDate se interpreta como fin exclusivo (día siguiente a 00:00).
    """
    start_d = date.fromisoformat(local_start_yyyy_mm_dd)
    end_d = date.fromisoformat(local_end_yyyy_mm_dd)

    tz = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))

    start_local = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=tz)
    # fin exclusivo: día siguiente 00:00
    end_local = datetime(end_d.year, end_d.month, end_d.day, 0, 0, 0, tzinfo=tz) + timedelta(days=1)

    start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return start_utc, end_utc

def _get_meeting_id_from_url(join_url: str) -> Optional[str]:
    """
    Extrae el ID de la reunión de Teams y lo decodifica para que coincida.
    """
    if not join_url:
        return None
    
    match = re.search(r'/meetup-join/([^/]+)', join_url)
    if match:
        encoded_id = match.group(1)
        # --- AQUÍ ESTÁ LA MAGIA ---
        # "Traducimos" el ID para que los caracteres especiales (%3a) se conviertan en normales (:)
        decoded_id = urllib.parse.unquote(encoded_id)
        return decoded_id
    return None

def _env(name: str, required: bool = True) -> str:
    v = os.getenv(name)
    if required and (not v):
        raise ValueError(f"Falta app setting requerido: {name}")
    return v or ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def acquire_graph_token() -> str:
    # OAuth2 client_credentials (app-only)
    now = int(time.time())
    if _TOKEN_CACHE["access_token"] and now < (_TOKEN_CACHE["expires_at"] - 60):
        return _TOKEN_CACHE["access_token"]

    tenant_id = _env("GRAPH_TENANT_ID")
    client_id = _env("GRAPH_CLIENT_ID")
    client_secret = _env("GRAPH_CLIENT_SECRET")
    scope = os.getenv("GRAPH_SCOPE", "https://graph.microsoft.com/.default")

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": scope,
    }

    resp = requests.post(token_url, data=data, timeout=30)
    if resp.status_code != 200:
        logging.error("Token error (%s): %s", resp.status_code, resp.text)
        raise RuntimeError(f"No se pudo obtener token: {resp.status_code} {resp.text}")

    payload = resp.json()
    _TOKEN_CACHE["access_token"] = payload["access_token"]
    _TOKEN_CACHE["expires_at"] = now + int(payload.get("expires_in", 3599))
    return _TOKEN_CACHE["access_token"]


def graph_get(url: str, token: str, params: Optional[Dict[str, str]] = None,
              extra_headers: Optional[Dict[str, str]] = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    return requests.get(url, headers=headers, params=params, timeout=60)


def _extract_request_id(text: str) -> Optional[str]:
    try:
        j = json.loads(text)
        return j.get("error", {}).get("innerError", {}).get("request-id")
    except Exception:
        return None


def graph_get_json_with_retry(url: str, token: str, params: Optional[Dict[str, str]] = None,
                              max_retries: int = 4) -> Dict[str, Any]:
    """
    Retry/backoff para UnknownError (Graph a veces lo devuelve sin detalle). [3](https://stackoverflow.com/questions/57929685/microsoft-graph-api-code-unknownerror)
    """
    delay = 1.0
    last_err = None

    for attempt in range(1, max_retries + 1):
        resp = graph_get(url, token, params=params)
        if resp.status_code < 300:
            return resp.json()

        req_id = _extract_request_id(resp.text)
        logging.error("Graph FAIL [%s] attempt=%s url=%s params=%s reqId=%s body=%s",
                      resp.status_code, attempt, url, params, req_id, resp.text)

        # Si es UnknownError, reintentar con backoff
        if resp.status_code == 400 and "UnknownError" in resp.text:
            last_err = resp
            time.sleep(delay)
            delay *= 2
            continue

        # otros errores: no reintentar
        raise RuntimeError(f"Graph GET failed {resp.status_code}: {resp.text}")

    # Si agotamos retries, devolvemos el último error
    if last_err is not None:
        raise RuntimeError(f"Graph GET failed 400 (UnknownError) after retries: {last_err.text}")

    raise RuntimeError("Graph GET failed: unknown state")


def get_all_transcripts_robust(
    organizer_user_id: str,
    token: str
) -> List[Dict[str, Any]]:
    """
    Llamada más robusta:
    GET /users/{usersId}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{userId}')
    Nota: la doc indica que falla si no pasas meetingOrganizerUserId (o filtro equivalente). [1](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-getalltranscripts?view=graph-rest-1.0)
    """
    u = organizer_user_id.strip()

    # Sin fechas en URL: reducimos riesgo de parsing/encoding
    url = f"{GRAPH_BASE}/users/{u}/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{u}')"

    transcripts: List[Dict[str, Any]] = []
    next_url = url

    while next_url:
        data = graph_get_json_with_retry(next_url, token)
        transcripts.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")

        # guardia
        if len(transcripts) > 5000:
            break

    return transcripts


def filter_transcripts_by_created_range(
    transcripts: List[Dict[str, Any]],
    start_dt: Optional[str],
    end_dt: Optional[str]
) -> List[Dict[str, Any]]:
    """
    Filtro Estricto: La transcripción debe haberse creado DENTRO de la reunión.
    """
    # Nivel 1
    if not transcripts or not start_dt or not end_dt:
        return []

    try:
        # Nivel 2 (Dentro del try)
        start_obj = _parse_dt(start_dt).replace(tzinfo=None)
        end_obj = _parse_dt(end_dt).replace(tzinfo=None)
        
        # Definimos variables que usará la función interna
        rango_inicio = start_obj - timedelta(minutes=1) 
        rango_fin = end_obj

    except Exception as e:
        logging.warning(f"Error parseando fechas filtro: {e}")
        return []

    # --- AQUÍ EMPIEZA LA FUNCIÓN INTERNA ---
    # Debe estar alineada al mismo nivel que el 'try' (Nivel 1) o dentro.
    # Python permite definir funciones dentro de funciones.
    def in_range(t: Dict[str, Any]) -> bool:
        # Nivel 2 (Dentro de in_range)
        c = t.get("createdDateTime")
        if not c:
            return False
        try:
            c_dt = _parse_dt(c).replace(tzinfo=None)
            return rango_inicio <= c_dt <= rango_fin
        except Exception:
            return False
    # --- AQUÍ TERMINA LA FUNCIÓN INTERNA ---

    # ¡ESTA ES LA LÍNEA IMPORTANTE!
    # Si te falta esta línea, VS Code pone 'in_range' en gris porque nadie la llama.
    # Debe estar alineada a la izquierda (Nivel 1), fuera del 'def in_range'.
    return [t for t in transcripts if in_range(t)]


def pick_transcript(
    transcripts: List[Dict[str, Any]],
    online_meeting_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """
    callTranscript trae meetingId y createdDateTime. [2](https://classic.yarnpkg.com/en/package/azure-functions-core-tools)
    """
    if not transcripts:
        return None

    if online_meeting_id:
        for t in transcripts:
            if t.get("meetingId") == online_meeting_id:
                return t

    def created_ts(t: Dict[str, Any]) -> float:
        s = t.get("createdDateTime")
        if not s:
            return 0.0
        try:
            return _parse_dt(s).timestamp()
        except Exception:
            return 0.0

    return sorted(transcripts, key=created_ts, reverse=True)[0]



import html

def download_transcript_vtt(
    organizer_user_id: str,
    online_meeting_id: str,
    transcript_id: str,
    token: str
) -> str:
    url = (
        f"{GRAPH_BASE}/users/{organizer_user_id}/onlineMeetings/{online_meeting_id}"
        f"/transcripts/{transcript_id}/content"
    )

    resp = graph_get(url, token, extra_headers={"Accept": "text/vtt"})

    if resp.status_code >= 300:
        raise RuntimeError(f"Descarga VTT falló {resp.status_code}: {resp.text}")

    # 1) Forzar UTF-8 (evita reuniÃ³n)
    resp.encoding = "utf-8"
    text = resp.text

    # 2) Des-escapar entidades HTML (evita &lt; &gt; y --&gt;)
    text = html.unescape(text)

    return text



@app.function_name(name="RecibirYDescargarTranscripcion")
@app.route(route="transcripts/download", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except Exception:
        body = {}

    organizer_user_id = body.get("meetingOrganizerUserId") or os.getenv("ORGANIZER_USER_ID")
    if not organizer_user_id:
        return func.HttpResponse(
            json.dumps({"error": "Falta meetingOrganizerUserId o configura ORGANIZER_USER_ID"}),
            status_code=400, mimetype="application/json"
        )

    online_meeting_id = body.get("onlineMeetingId")
    
    start_dt = body.get("startDateTime")
    end_dt = body.get("endDateTime")

    start_local = body.get("startLocalDate")
    end_local = body.get("endLocalDate")

    if start_local and end_local:
        start_dt, end_dt = local_date_range_to_utc(start_local, end_local)

    # buffer opcional para evitar que el transcript caiga “justo después”
    # (muy útil por delays de disponibilidad)
    BUFFER_HOURS = int(os.getenv("TRANSCRIPT_END_BUFFER_HOURS", "2"))
    try:
        end_dt_obj = _parse_dt(end_dt)
        end_dt = (end_dt_obj + timedelta(hours=BUFFER_HOURS)).isoformat().replace("+00:00", "Z")
    except Exception:
        pass

    # Default: últimas 24h si no vienen fechas
    if not start_dt or not end_dt:
        now = _utc_now()
        start_dt = start_dt or (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        end_dt = end_dt or now.isoformat().replace("+00:00", "Z")

    try:
        token = acquire_graph_token()

        # 1) Traer todos los transcripts del organizer (robusto)
        transcripts = get_all_transcripts_robust(organizer_user_id, token)

        # 2) Filtrar por rango localmente (createdDateTime)
        transcripts = filter_transcripts_by_created_range(transcripts, start_dt, end_dt)

        chosen = pick_transcript(transcripts, online_meeting_id)
        if not chosen:
            return func.HttpResponse(
                json.dumps({
                    "message": "No se encontraron transcripciones en el rango dado.",
                    "count": 0,
                    "rangeUsed": {"startDateTime": start_dt, "endDateTime": end_dt}
                }),
                status_code=404, mimetype="application/json"
            )

        transcript_id = chosen.get("id")
        meeting_id = chosen.get("meetingId")

        if not transcript_id or not meeting_id:
            return func.HttpResponse(
                json.dumps({"error": "Transcript encontrado pero faltan id/meetingId"}),
                status_code=500, mimetype="application/json"
            )

        vtt = download_transcript_vtt(organizer_user_id, meeting_id, transcript_id, token)

        return func.HttpResponse(
            json.dumps({
                "message": "OK",
                "selectedTranscript": {
                    "id": transcript_id,
                    "meetingId": meeting_id,
                    "createdDateTime": chosen.get("createdDateTime")
                },
                "rangeUsed": {"startDateTime": start_dt, "endDateTime": end_dt},
                "vtt": vtt
            }),
            status_code=200, mimetype="application/json"
        )

    except Exception as e:
        logging.exception("Error procesando transcript")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500, mimetype="application/json"
        )
# =================================================================================
#  NUEVO BLOQUE: AUTOMATIZACIÓN (TIMER, CALENDARIO Y MEMORIA)
#  Pegar esto al final de function_app.py
# =================================================================================

from azure.data.tables import TableClient
from azure.core.exceptions import ResourceNotFoundError

# 1. GESTIÓN DE MEMORIA (Table Storage)
def get_table_client():
    conn_str = _env("STORAGE_CONNECTION")
    return TableClient.from_connection_string(conn_str, table_name="ReunionesProcesadas")

def reunion_ya_procesada(meeting_id: str) -> bool:
    try:
        client = get_table_client()
        client.get_entity(partition_key="comite", row_key=meeting_id)
        logging.info(f"Reunión {meeting_id} ya existe en el historial. Saltando.")
        return True
    except ResourceNotFoundError:
        return False
    except Exception as e:
        logging.error(f"Error consultando tabla: {e}")
        return False # Ante la duda, intentamos procesar

def marcar_reunion_procesada(meeting_id: str, subject: str):
    try:
        client = get_table_client()
        entity = {
            "PartitionKey": "comite",
            "RowKey": meeting_id,
            "Asunto": subject,
            "FechaProceso": datetime.utcnow().isoformat()
        }
        client.upsert_entity(entity)
        logging.info(f"Reunión {meeting_id} marcada como procesada.")
    except Exception as e:
        logging.error(f"Error guardando en tabla: {e}")

# 2. CONSULTA AL CALENDARIO (CORREGIDA)
def get_finished_meetings_from_calendar(organizer_email: str, token: str) -> List[Dict[str, Any]]:
    # Usamos utcnow() que es "Naive" (sin zona horaria explicita, pero es UTC)
    now = datetime.utcnow()
    
    # MODO PRUEBA: Buscamos 12 horas atrás para asegurar que detecte tus pruebas
    start_lookback = now - timedelta(hours=18) 
    
    # Formato ISO estricto para la URL de Graph
    start_str = start_lookback.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"{GRAPH_BASE}/users/{organizer_email}/calendar/calendarView"
        f"?startDateTime={start_str}&endDateTime={end_str}"
    )
    
    logging.info(f"Consultando calendario entre {start_str} y {end_str}")
    
    try:
        data = graph_get_json_with_retry(url, token)
        events = data.get("value", [])
        
        finished_events = []
        
        # Preparamos el límite "ahora" asegurándonos de que sea comparable
        # Convertimos 'now' a string y luego lo parseamos con tu función para que sea igual al evento
        limit_obj = _parse_dt(end_str)
        # TRUCO: Le quitamos la zona horaria para hacerlo "Naive" y evitar el error
        if limit_obj.tzinfo is not None:
            limit_obj = limit_obj.replace(tzinfo=None)

        for e in events:
            # Obtenemos fecha fin del evento
            end_dt_str = e["end"]["dateTime"]
            try:
                event_end_obj = _parse_dt(end_dt_str)
                
                # TRUCO: También le quitamos la zona horaria al evento
                if event_end_obj.tzinfo is not None:
                    event_end_obj = event_end_obj.replace(tzinfo=None)
                
                # Ahora ambas son "Naive" y Python felizmente las compara
                if e.get("isOnlineMeeting") is True and event_end_obj <= limit_obj:
                    finished_events.append(e)
            except Exception as parse_err:
                logging.warning(f"No se pudo parsear fecha de evento: {parse_err}")
                continue
                
        return finished_events
    except Exception as e:
        logging.error(f"Error leyendo calendario: {e}")
        return []

# =================================================================================
#  BLOQUE DE INTELIGENCIA ARTIFICIAL (AZURE OPENAI)
# =================================================================================
import openai

def analizar_transcripcion_con_ia(texto_vtt: str) -> Dict[str, Any]:
    """
    Envía el texto a GPT y retorna un diccionario estructurado.
    Versión Blindada: Inicializa cliente interno y limpia JSON con Regex.
    """
    
    # 1. Obtener credenciales
    endpoint = _env("AZURE_OPENAI_ENDPOINT")
    key = _env("AZURE_OPENAI_KEY")
    deployment = _env("AZURE_OPENAI_DEPLOYMENT")
    
    # 2. INICIALIZAR EL CLIENTE (Esto es lo que faltaba)
    client = openai.AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=key,
        api_version="2025-12-26"
    )

    # 3. Prompt
    system_prompt = """
    Eres un secretario experto. Analiza la transcripción adjunta.
    Tu misión: Extraer un resumen y la lista de acuerdos/tareas.
    
    IMPORTANTE:
    - Si el orador no es claro, usa el contexto para deducir quién es (Ej: "Carlos, hazlo tú" -> Responsable: Carlos).
    - RESPONDE ÚNICAMENTE CON UN JSON VÁLIDO. NO ESCRIBAS TEXTO INTRODUCTORIO.
    
    Estructura JSON requerida:
    {
        "resumen": "Resumen ejecutivo del comité...",
        "acuerdos": [
            {
                "titulo": "Título de la tarea",
                "descripcion": "Detalle completo",
                "responsable_sugerido": "Nombre detectado",
                "fecha_limite": "Fecha o 'No mencionada'"
            }
        ]
    }
    """

    try:
        # 4. Llamada a la IA
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": texto_vtt}
            ],
            max_completion_tokens=2000, # Ajuste para GPT-5/o1
            temperature=1 
        )
        
        content = response.choices[0].message.content
        
        # 5. Limpieza de JSON con Regex (Anti-Bucle)
        # Busca el primer '{' y el último '}' para ignorar texto basura antes o después
        match = re.search(r'\{[\s\S]*\}', content)
        
        if match:
            json_limpio = match.group(0)
            return json.loads(json_limpio)
        else:
            logging.error(f"FATAL: La IA no devolvió un JSON válido. Respuesta: {content[:100]}...")
            return None

    except Exception as e:
        logging.error(f"Error crítico conectando con OpenAI: {e}")
        return None

# 3. EL DISPARADOR AUTOMÁTICO (TIMER TRIGGER) - VERSIÓN FINAL DE PRODUCCIÓN CON LOG DE IA
@app.schedule(schedule="0 */2 * * * *", arg_name="myTimer", run_on_startup=False, use_monitor=False) 
def TimerComite(myTimer: func.TimerRequest) -> None:
    if myTimer.past_due:
        logging.warning('El timer va con retraso!')

    logging.info('--- INICIANDO CICLO DE PROCESAMIENTO DE PRODUCCIÓN ---')
    
    organizer_id = _env("ORGANIZER_USER_ID")
    
    try:
        token = acquire_graph_token()
        
        meetings = get_finished_meetings_from_calendar(organizer_id, token)
        if meetings:
            logging.info(f"Encontradas {len(meetings)} reuniones finalizadas en el calendario.")
        
        all_transcripts = get_all_transcripts_robust(organizer_id, token)
        if all_transcripts:
            logging.info(f"Se obtuvieron {len(all_transcripts)} transcripciones históricas para filtrar.")

        for m in meetings:
            subject = m.get("subject", "Sin Asunto")
            event_id = m.get("id") 
            
            if reunion_ya_procesada(event_id):
                continue
                
            logging.info(f"--- ANALIZANDO: {subject} ---")
            
            join_url = m.get("onlineMeeting", {}).get("joinUrl")
            target_meeting_id_decoded = _get_meeting_id_from_url(join_url)

            if not target_meeting_id_decoded:
                logging.warning(f"No se pudo extraer el ID de la reunión para '{subject}'. Saltando.")
                continue

            chosen = None
            for transcript in all_transcripts:
                transcript_id_full = transcript.get("meetingId", "")
                try:
                    if transcript_id_full.startswith('1*'):
                        base64_part = transcript_id_full[2:]
                    else:
                        base64_part = transcript_id_full
                    
                    base64_part += '=' * (-len(base64_part) % 4)
                    decoded_from_full_id = base64.b64decode(base64_part).decode('utf-8', errors='ignore')
                    
                    if target_meeting_id_decoded in decoded_from_full_id:
                        chosen = transcript
                        break
                except Exception:
                    continue
            
            if chosen:
                t_id = chosen.get("id")
                m_id = chosen.get("meetingId")
                
                logging.info(f"¡VINCULACIÓN EXITOSA! Procesando transcripción para '{subject}'.")
                
                texto_vtt = download_transcript_vtt(organizer_id, m_id, t_id, token)
                
                if texto_vtt:
                    logging.info(f"Texto descargado ({len(texto_vtt)} caracteres). Procesando con IA...")
                    
                    datos_ia = analizar_transcripcion_con_ia(texto_vtt)
                    
                    if datos_ia:
                        # --- LOG DE VERIFICACIÓN DE IA ---
                        import json
                        json_bonito = json.dumps(datos_ia, indent=4, ensure_ascii=False)
                        logging.info(f"\n--- RESULTADO DE IA PARA '{subject}' ---\n{json_bonito}\n------------------------------------------")
                        # ---------------------------------

                        logging.info("Enviando a ClickUp...")
                        
                        target_list_id = _env("CLICKUP_LIST_ID")
                        resumen_texto = datos_ia.get("resumen", "Sin resumen")
                        task_madre_id = crear_tarea_madre(target_list_id, subject, resumen_texto)
                        
                        if task_madre_id:
                            lista_acuerdos = datos_ia.get("acuerdos", [])
                            crear_subtareas_acuerdos(task_madre_id, target_list_id, lista_acuerdos)
                            
                            marcar_reunion_procesada(event_id, subject)
                            logging.warning(f"✅ CICLO PARA '{subject}' COMPLETADO Y ENVIADO A CLICKUP.")
                        else:
                            logging.error(f"Fallo al crear tarea en ClickUp para '{subject}'. Se reintentará en el próximo ciclo.")
                    else:
                        logging.error(f"La IA no devolvió datos válidos para '{subject}'. Se reintentará.")
                else:
                    logging.warning(f"El contenido VTT estaba vacío para '{subject}'. Se reintentará.")
            else:
                logging.info(f"Aún no hay transcripción disponible con el ID correspondiente para '{subject}'.")

    except Exception as e:
        logging.error(f"Error crítico en el ciclo del Timer: {e}", exc_info=True)

