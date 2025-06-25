from datetime import datetime, timedelta
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
    
    @staticmethod
    def get_relative_time_string(timestamp: datetime, now: datetime = None) -> str:
        """
        get a relative time string for a timestamp compared to now
        
        format:
        - within the last hour: "X minutes ago"
        - today, but more than an hour ago: "X hours ago" or "this morning/afternoon/evening"
        - yesterday: "yesterday morning/afternoon/evening"
        - within the last week: "X days ago"
        - more than a week ago: "on Month Day"
        """
        if now is None:
            now = datetime.now()
            
        # calculate the time difference
        diff = now - timestamp
        
        # within the last hour
        if diff < timedelta(hours=1):
            minutes = int(diff.total_seconds() / 60)
            if minutes < 1:
                return "just now"
            elif minutes == 1:
                return "1 minute ago"
            else:
                return f"{minutes} minutes ago"
        
        # today, but more than an hour ago
        elif timestamp.date() == now.date():
            hours = int(diff.total_seconds() / 3600)
            if hours == 1:
                return "1 hour ago"
            elif hours < 4:
                return f"{hours} hours ago"
            else:
                # use time of day for older messages today
                time_of_day = TemporalContext.get_time_of_day(timestamp)
                return f"this {time_of_day}"
        
        # yesterday
        elif timestamp.date() == (now - timedelta(days=1)).date():
            time_of_day = TemporalContext.get_time_of_day(timestamp)
            return f"yesterday {time_of_day}"
        
        # within the last week
        elif diff < timedelta(days=7):
            days = diff.days
            if days == 1:
                return "1 day ago"
            else:
                return f"{days} days ago"
        
        # more than a week ago
        else:
            # format as "on Month Day" (e.g., "on June 15th")
            day = timestamp.day
            # add ordinal suffix
            if 10 <= day % 100 <= 20:
                suffix = "th"
            else:
                suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            
            return f"on {timestamp.strftime('%B')} {day}{suffix}"
    
    @staticmethod
    def format_message_with_temporal_context(
        content: str, 
        role: str, 
        timestamp: datetime, 
        user_name: str = None,
        now: datetime = None
    ) -> str:
        """
        format a message with temporal context prepended
        
        format: "name (time ago): content"
        """
        # get the relative time string
        time_string = TemporalContext.get_relative_time_string(timestamp, now)
        
        # determine the name to use
        if role == "user":
            name = user_name or "user"
        else:
            name = "chordial"
        
        # combine into the final format
        return f"{name} ({time_string}): {content}"