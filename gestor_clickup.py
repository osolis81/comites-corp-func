import os
import requests
import logging

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
    """
    url = f"{CLICKUP_API_URL}/list/{list_id}/task"
    
    # Preparamos el cuerpo de la tarea con Markdown para que se vea bien
    body = {
        "name": f"Acta del Comité: {titulo_reunion}",
        "description": f"**Resumen Ejecutivo (Generado por IA):**\n\n{resumen_ia}",
        "status": "To Do" # Puedes cambiar esto al estado inicial que uses
    }
    
    try:
        resp = requests.post(url, headers=_get_headers(), json=body)
        
        if resp.status_code == 200:
            task_id = resp.json().get("id")
            logging.info(f"Tarea Madre creada exitosamente en ClickUp. ID: {task_id}")
            return task_id
        else:
            logging.error(f"Error creando Tarea Madre en ClickUp ({resp.status_code}): {resp.text}")
            return None
    except Exception as e:
        logging.error(f"Excepción crítica al conectar con ClickUp: {e}")
        return None

def crear_subtareas_acuerdos(parent_task_id: str, list_id: str, acuerdos_json: list):
    """
    Recorre la lista de acuerdos de la IA y crea una subtarea por cada uno.
    """
    url = f"{CLICKUP_API_URL}/list/{list_id}/task"
    
    contador_exitos = 0
    for acuerdo in acuerdos_json:
        titulo_acuerdo = acuerdo.get("titulo", "Acuerdo sin título")
        responsable_texto = acuerdo.get("responsable_sugerido", "POR DEFINIR")
        descripcion_acuerdo = acuerdo.get("descripcion", "Sin descripción detallada.")
        
        # Como aún no mapeamos nombres a IDs de ClickUp, ponemos el nombre en la descripción
        # para que se pueda asignar manualmente con un clic.
        descripcion_subtarea = (
            f"**Responsable Sugerido por IA:** {responsable_texto}\n\n"
            f"**Detalle del Acuerdo:**\n{descripcion_acuerdo}"
        )
        
        body = {
            "name": titulo_acuerdo,
            "description": descripcion_subtarea,
            "parent": parent_task_id  # La magia que la convierte en subtarea
        }
        
        try:
            resp = requests.post(url, headers=_get_headers(), json=body)
            if resp.status_code == 200:
                contador_exitos += 1
            else:
                logging.error(f"Error creando subtarea '{titulo_acuerdo}': {resp.text}")
        except Exception as e:
            logging.error(f"Excepción creando subtarea: {e}")
            
    logging.info(f"Se crearon {contador_exitos} de {len(acuerdos_json)} subtareas en ClickUp.")