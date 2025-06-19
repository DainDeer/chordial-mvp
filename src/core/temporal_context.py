from datetime import datetime
from typing import Dict
from dataclasses import dataclass

@dataclass
class TemporalContext:
    """provides rich temporal context for ai interactions"""
    
    def __init__(self):
        self.now = datetime.now()
    
    def get_time_of_day(self) -> str:
        """return morning, afternoon, evening, or night"""
        hour = self.now.hour
        if 5 <= hour < 12:
            return "morning"
        elif 12 <= hour < 17:
            return "afternoon"
        elif 17 <= hour < 21:
            return "evening"
        else:
            return "night"
    
    def get_day_context(self) -> str:
        """return weekday or weekend"""
        return "weekend" if self.now.weekday() >= 5 else "weekday"
    
    def get_detailed_context(self) -> Dict[str, str]:
        """get all temporal context as a dictionary"""
        return {
            "current_time": self.now.strftime("%I:%M %p"),
            "date": self.now.strftime("%A, %B %d, %Y"),
            "time_of_day": self.get_time_of_day(),
            "day_type": self.get_day_context(),
            "day_of_week": self.now.strftime("%A").lower(),
            "hour_24": self.now.hour,
            "month": self.now.strftime("%B").lower()
        }
    
    def get_context_string(self) -> str:
        """get a natural language description of current time"""
        context = self.get_detailed_context()
        return (f"it's {context['current_time']} on {context['date']}. "
                f"it's a {context['day_type']} {context['time_of_day']}.")
    
    def get_special_context(self) -> str:
        """return any special temporal context (friday vibes, monday blues, etc)"""
        day = self.now.strftime("%A").lower()
        hour = self.now.hour
        
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
        if self.get_day_context() == "weekend" and 7 <= hour < 11:
            return "it's a weekend morning - perfect for relaxed activities"
        
        return ""