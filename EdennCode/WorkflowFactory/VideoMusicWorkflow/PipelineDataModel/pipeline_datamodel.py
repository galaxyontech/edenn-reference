from dataclasses import dataclass



@dataclass
class Language:
    CN: str = "CHINESE_MAINLAND"
    EN: str = "ENGLISH_US"


@dataclass
class VideoCategory:
    ADVERTISEMENT: str = "ADVERTISEMENT"
    VLOG: str = "VLOG"
    CREATOR_CONTENT: str = "CREATOR_CONTENT"
    DEFAULT: str = "VIDEO"


@dataclass
class VideoMusicUserUnderstandingPayload:
    language: str = Language.EN
    video_category: str = VideoCategory.ADVERTISEMENT
