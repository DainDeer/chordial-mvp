from datetime import datetime
from typing import Dict

class TemporalContext:
    """provides rich temporal context for ai interactions using static methods"""
    
    @staticmethod
    def get_time_of_day(timestamp: datetime) -> str:
        """return morning, afternoon, evening, or night"""
        hour = timestamp.hour
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"
    
    @staticmethod
    def get_day_context(timestamp: datetime) -> str:
        """return weekday or weekend"""
        return "weekend" if timestamp.weekday() >= 5 else "weekday"
    
    @staticmethod
    def get_detailed_context(timestamp: datetime) -> Dict[str, str]:
        """get all temporal context as a dictionary"""
        return {
            "current_time": timestamp.strftime("%I:%M %p"),
            "date": timestamp.strftime("%A, %B %d, %Y"),
            "time_of_day": TemporalContext.get_time_of_day(timestamp),
            "day_type": TemporalContext.get_day_context(timestamp),
            "day_of_week": timestamp.strftime("%A").lower(),
            "hour_24": timestamp.hour,
            "month": timestamp.strftime("%B").lower()
        }
    
    @staticmethod
    def get_context_string(timestamp: datetime) -> str:
        """get a natural language description of current time"""
        context = TemporalContext.get_detailed_context(timestamp)
        return (f"it's {context['current_time']} on {context['date']}. "
                f"it's a {context['day_type']} {context['time_of_day']}.")
    
    @staticmethod
    def get_special_context(timestamp: datetime) -> str:
        """return any special temporal context (friday vibes, monday blues, etc)"""
        day = timestamp.strftime("%A").lower()
        hour = timestamp.hour
        day_context = TemporalContext.get_day_context(timestamp)
        
        # friday afternoon/evening
        if day == "friday" and hour >= 15:
            return "it's friday afternoon - weekend vibes incoming!"
        
        # monday morning
        if day == "monday" and 6 <= hour < 12:
            return "it's monday morning - fresh start to the week"
        
        # late night any day
        if hour >= 23 or hour < 3:
            return "it's pretty late - might be time to wind down soon"
        
        # weekend morning
        if day_context == "weekend" and 7 <= hour < 11:
            return "it's a weekend morning - perfect for relaxed activities"
        
        return ""