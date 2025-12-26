import os
import requests
import logging
import math
import time

# URL base de la API de ClickUp
CLICKUP_API_URL = "https://api.clickup.com/api/v2"

def _get_headers():
    """Prepara la cabecera de autenticación para todas las llamadas a ClickUp."""
    token = os.environ.get("CLICKUP_API_KEY")
    if not token:
        raise ValueError("La variable de entorno CLICKUP_API_KEY no está configurada.")
    return {
        "Authorization": token,
        "Content-Type": "application/json"
    }

def crear_tarea_madre(list_id: str, titulo_reunion: str, resumen_ia: str) -> str or None:
    """
    Crea la tarea principal que contendrá el acta y las subtareas.
    Retorna el ID de la tarea creada si tiene éxito, o None si falla.
    (Esta función no cambia, sigue siendo 1 sola llamada a la API)
    """
    url = f"{CLICKUP_API_URL}/list/{list_id}/task"
    body = {
        "name": f"Acta del Comité: {titulo_reunion}",
        "description": f"**Resumen Ejecutivo (Generado por IA):**\n\n{resumen_ia}",
        "status": "To Do"
    }
    
    try:
        resp = requests.post(url, headers=_get_headers(), json=body)
        if resp.status_code == 200:
            task_id = resp.json().get("id")
            logging.info(f"Tarea Madre creada exitosamente en ClickUp. ID: {task_id}")
            return task_id
        else:
            # Si el error es 429, esperamos un minuto antes de continuar
            if resp.status_code == 429:
                logging.warning("Límite de API alcanzado en Tarea Madre. Esperando 60 segundos...")
                time.sleep(60)
            logging.error(f"Error creando Tarea Madre en ClickUp ({resp.status_code}): {resp.text}")
            return None
    except Exception as e:
        logging.error(f"Excepción crítica al conectar con ClickUp: {e}")
        return None

def crear_subtareas_acuerdos(parent_task_id: str, list_id: str, acuerdos_json: list):
    """
    VERSIÓN BLINDADA: Crea subtareas una por una con protección proactiva (pausa) 
    y reactiva (reintento en error 429).
    """
    if not acuerdos_json:
        logging.info("No se detectaron acuerdos para crear subtareas.")
        return

    url = f"{CLICKUP_API_URL}/list/{list_id}/task"
    
    contador_exitos = 0
    for i, acuerdo in enumerate(acuerdos_json):
        titulo_acuerdo = acuerdo.get("titulo", "Acuerdo sin título")
        responsable_texto = acuerdo.get("responsable_sugerido", "POR DEFINIR")
        descripcion_acuerdo = acuerdo.get("descripcion", "Sin descripción detallada.")
        
        descripcion_subtarea = (
            f"**Responsable Sugerido por IA:** {responsable_texto}\n\n"
            f"**Detalle del Acuerdo:**\n{descripcion_acuerdo}"
        )
        
        body = {
            "name": titulo_acuerdo,
            "description": descripcion_subtarea,
            "parent": parent_task_id,
        }
        
        try:
            # 1. Protección Proactiva: Pausa entre llamadas
            # Esto evita que lleguemos al límite en primer lugar.
            if i > 0:
                time.sleep(0.7)

            logging.info(f"Creando subtarea {i+1}/{len(acuerdos_json)}: '{titulo_acuerdo}'...")
            
            resp = requests.post(url, headers=_get_headers(), json=body)
            
            # 2. Protección Reactiva: Manejo del error 429 (Límite de API)
            if resp.status_code == 429:
                logging.warning("Límite de API de ClickUp alcanzado. Pausando 60 segundos antes de reintentar...")
                time.sleep(60)
                
                logging.info(f"Reintentando creación de subtarea '{titulo_acuerdo}'...")
                resp = requests.post(url, headers=_get_headers(), json=body) # Segundo intento

            # Verificación final del resultado
            if resp.status_code == 200:
                contador_exitos += 1
            else:
                logging.error(f"FALLO DEFINITIVO al crear subtarea '{titulo_acuerdo}' ({resp.status_code}): {resp.text}")

        except Exception as e:
            logging.error(f"Excepción crítica creando subtarea: {e}")
            
    logging.info(f"PROCESO FINALIZADO: Se crearon {contador_exitos} de {len(acuerdos_json)} subtareas en ClickUp.")