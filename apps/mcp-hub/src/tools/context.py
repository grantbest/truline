import httpx
from datetime import datetime

def get_current_context() -> dict:
    """Returns location, weather, and day context.
    Location: Chicago (Mon-Wed) or Elgin (Thu-Sun).
    Weather: from Open-Meteo API, no key required.
    Returns: { location, day_type, weather: { temp, conditions, forecast } }
    """
    now = datetime.now()
    day_of_week = now.weekday()  # 0 is Monday, 6 is Sunday
    
    if day_of_week <= 2:
        location = "Chicago"
        lat, lon = 41.8827, -87.6233
        day_type = "office"
    else:
        location = "Elgin"
        lat, lon = 42.0354, -88.2826
        day_type = "home"
        
    weather_data = {}
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&daily=temperature_2m_max,temperature_2m_min&timezone=America%2FChicago"
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            weather_data = resp.json()
    except Exception as e:
        weather_data = {"error": str(e)}

    return {
        "location": location,
        "day_type": day_type,
        "weather": {
            "temp": weather_data.get("current_weather", {}).get("temperature"),
            "conditions": weather_data.get("current_weather", {}).get("weathercode"),
            "forecast": weather_data.get("daily", {})
        }
    }
