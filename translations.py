# translations.py — Lightweight i18n for Omnisconn dashboard & emails
#
# Auto-detected from browser Accept-Language header; user can override via topbar.
# Only UI strings are translated — brand name, CRM names, and technical terms stay English.

SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "pt": "Português",
    "zh": "中文",
    "ko": "한국어",
    "ja": "日本語",
    "vi": "Tiếng Việt",
    "hi": "हिन्दी",
    "tl": "Tagalog",
}

# ─── Translation strings keyed by dotted path ─────────────────────────────
# Usage: _t("sidebar.dashboard", lang) → "Dashboard" / "Tablero" / etc.

_STRINGS = {
    # ── Sidebar ──
    "sidebar.dashboard": {
        "en": "Dashboard", "es": "Tablero", "fr": "Tableau de bord", "pt": "Painel",
        "zh": "仪表板", "ko": "대시보드", "ja": "ダッシュボード", "vi": "Bảng điều khiển",
        "hi": "डैशबोर्ड", "tl": "Dashboard",
    },
    "sidebar.bot_config": {
        "en": "Bot Config", "es": "Config. del Bot", "fr": "Config. du Bot", "pt": "Config. do Bot",
        "zh": "机器人配置", "ko": "봇 설정", "ja": "ボット設定", "vi": "Cấu hình Bot",
        "hi": "बॉट कॉन्फ़िग", "tl": "Config ng Bot",
    },
    "sidebar.voice": {
        "en": "Voice AI", "es": "Voz IA", "fr": "Voix IA", "pt": "Voz IA",
        "zh": "语音AI", "ko": "음성 AI", "ja": "音声AI", "vi": "Giọng nói AI",
        "hi": "वॉइस AI", "tl": "Voice AI",
    },
    "sidebar.dialer": {
        "en": "Dialer", "es": "Marcador", "fr": "Composeur", "pt": "Discador",
        "zh": "拨号器", "ko": "다이얼러", "ja": "ダイヤラー", "vi": "Trình gọi",
        "hi": "डायलर", "tl": "Dialer",
    },
    "sidebar.carriers": {
        "en": "Carriers", "es": "Aseguradoras", "fr": "Assureurs", "pt": "Seguradoras",
        "zh": "保险公司", "ko": "보험사", "ja": "キャリア", "vi": "Nhà bảo hiểm",
        "hi": "बीमा कंपनियाँ", "tl": "Mga Carrier",
    },
    "sidebar.connect": {
        "en": "Connect CRM", "es": "Conectar CRM", "fr": "Connecter CRM", "pt": "Conectar CRM",
        "zh": "连接CRM", "ko": "CRM 연결", "ja": "CRM接続", "vi": "Kết nối CRM",
        "hi": "CRM कनेक्ट", "tl": "Ikonekta CRM",
    },
    "sidebar.advanced": {
        "en": "Advanced", "es": "Avanzado", "fr": "Avancé", "pt": "Avançado",
        "zh": "高级", "ko": "고급", "ja": "詳細設定", "vi": "Nâng cao",
        "hi": "उन्नत", "tl": "Advanced",
    },
    "sidebar.billing": {
        "en": "Billing", "es": "Facturación", "fr": "Facturation", "pt": "Faturamento",
        "zh": "账单", "ko": "결제", "ja": "請求", "vi": "Thanh toán",
        "hi": "बिलिंग", "tl": "Billing",
    },
    "sidebar.logs": {
        "en": "Activity Logs", "es": "Registro de actividad", "fr": "Journaux", "pt": "Registros",
        "zh": "活动日志", "ko": "활동 로그", "ja": "アクティビティログ", "vi": "Nhật ký",
        "hi": "गतिविधि लॉग", "tl": "Activity Logs",
    },
    "sidebar.ai_minutes": {
        "en": "AI Minutes", "es": "Minutos IA", "fr": "Minutes IA", "pt": "Minutos IA",
        "zh": "AI分钟数", "ko": "AI 분", "ja": "AI分", "vi": "Phút AI",
        "hi": "AI मिनट", "tl": "AI Minutes",
    },

    # ── Paywall ──
    "paywall.title": {
        "en": "Subscription Required", "es": "Suscripción requerida", "fr": "Abonnement requis",
        "pt": "Assinatura necessária", "zh": "需要订阅", "ko": "구독 필요",
        "ja": "サブスクリプションが必要です", "vi": "Cần đăng ký",
        "hi": "सदस्यता आवश्यक", "tl": "Kailangan ng Subscription",
    },
    "paywall.body": {
        "en": "You've successfully connected your Lead Connector account! To activate your bot and start using all features, please subscribe to the Individual Plan.",
        "es": "¡Has conectado tu cuenta de Lead Connector! Para activar tu bot y usar todas las funciones, suscríbete al Plan Individual.",
        "fr": "Vous avez connecté votre compte Lead Connector ! Pour activer votre bot et utiliser toutes les fonctionnalités, abonnez-vous au Plan Individuel.",
        "pt": "Você conectou sua conta Lead Connector! Para ativar seu bot e usar todos os recursos, assine o Plano Individual.",
        "zh": "您已成功连接Lead Connector账户！要激活您的机器人并使用所有功能，请订阅个人计划。",
        "ko": "Lead Connector 계정이 연결되었습니다! 봇을 활성화하고 모든 기능을 사용하려면 개인 플랜에 가입하세요.",
        "ja": "Lead Connectorアカウントの接続に成功しました！ボットを有効にしてすべての機能を使用するには、個人プランにご登録ください。",
        "vi": "Bạn đã kết nối tài khoản Lead Connector! Để kích hoạt bot và sử dụng tất cả tính năng, vui lòng đăng ký Gói Cá nhân.",
        "hi": "आपने अपना Lead Connector खाता सफलतापूर्वक कनेक्ट कर लिया है! अपने बॉट को सक्रिय करने के लिए कृपया व्यक्तिगत योजना की सदस्यता लें।",
        "tl": "Matagumpay mong nakonekta ang iyong Lead Connector account! Para ma-activate ang iyong bot, mag-subscribe sa Individual Plan.",
    },
    "paywall.subscribe_btn": {
        "en": "Subscribe Now - Individual Plan", "es": "Suscribirse - Plan Individual",
        "fr": "S'abonner - Plan Individuel", "pt": "Assinar - Plano Individual",
        "zh": "立即订阅 - 个人计划", "ko": "지금 구독 - 개인 플랜",
        "ja": "今すぐ登録 - 個人プラン", "vi": "Đăng ký ngay - Gói Cá nhân",
        "hi": "अभी सदस्यता लें - व्यक्तिगत योजना", "tl": "Mag-subscribe - Individual Plan",
    },
    "paywall.cancel_note": {
        "en": "Cancel anytime \u2022 No long-term contracts",
        "es": "Cancela en cualquier momento \u2022 Sin contratos",
        "fr": "Annulez à tout moment \u2022 Sans engagement",
        "pt": "Cancele a qualquer momento \u2022 Sem contratos",
        "zh": "随时取消 \u2022 无长期合同", "ko": "언제든 취소 \u2022 장기 계약 없음",
        "ja": "いつでもキャンセル可能 \u2022 長期契約なし",
        "vi": "Hủy bất kỳ lúc nào \u2022 Không hợp đồng dài hạn",
        "hi": "कभी भी रद्द करें \u2022 कोई दीर्घकालिक अनुबंध नहीं",
        "tl": "I-cancel kahit kailan \u2022 Walang kontrata",
    },

    # ── Common buttons / labels ──
    "common.save": {
        "en": "Save", "es": "Guardar", "fr": "Enregistrer", "pt": "Salvar",
        "zh": "保存", "ko": "저장", "ja": "保存", "vi": "Lưu",
        "hi": "सहेजें", "tl": "I-save",
    },
    "common.cancel": {
        "en": "Cancel", "es": "Cancelar", "fr": "Annuler", "pt": "Cancelar",
        "zh": "取消", "ko": "취소", "ja": "キャンセル", "vi": "Hủy",
        "hi": "रद्द करें", "tl": "I-cancel",
    },
    "common.loading": {
        "en": "Loading...", "es": "Cargando...", "fr": "Chargement...", "pt": "Carregando...",
        "zh": "加载中...", "ko": "로딩 중...", "ja": "読み込み中...", "vi": "Đang tải...",
        "hi": "लोड हो रहा है...", "tl": "Naglo-load...",
    },

    # ── Setup alerts ──
    "alert.set_password": {
        "en": "Set your password so you can log in later",
        "es": "Establece tu contraseña para poder iniciar sesión más tarde",
        "fr": "Définissez votre mot de passe pour vous connecter plus tard",
        "pt": "Defina sua senha para poder fazer login depois",
        "zh": "设置密码以便以后登录", "ko": "나중에 로그인할 수 있도록 비밀번호를 설정하세요",
        "ja": "後でログインできるようにパスワードを設定してください",
        "vi": "Đặt mật khẩu để đăng nhập sau",
        "hi": "बाद में लॉगिन करने के लिए अपना पासवर्ड सेट करें",
        "tl": "I-set ang password mo para makapag-login mamaya",
    },
    "alert.connect_calendar": {
        "en": "Your bot can't book appointments until a calendar is linked.",
        "es": "Tu bot no puede agendar citas hasta que conectes un calendario.",
        "fr": "Votre bot ne peut pas réserver de rendez-vous tant qu'un calendrier n'est pas lié.",
        "pt": "Seu bot não pode agendar compromissos até que um calendário esteja vinculado.",
        "zh": "在连接日历之前，您的机器人无法预约。",
        "ko": "캘린더가 연결될 때까지 봇이 예약을 잡을 수 없습니다.",
        "ja": "カレンダーがリンクされるまで、ボットは予約できません。",
        "vi": "Bot của bạn không thể đặt lịch hẹn cho đến khi liên kết lịch.",
        "hi": "आपका बॉट तब तक अपॉइंटमेंट बुक नहीं कर सकता जब तक कैलेंडर लिंक न हो।",
        "tl": "Hindi maka-book ng appointment ang bot mo hangga't walang nakalink na calendar.",
    },
    "alert.select_carriers": {
        "en": "Tell the bot which insurance carriers you're contracted with.",
        "es": "Dile al bot con qué aseguradoras tienes contrato.",
        "fr": "Indiquez au bot avec quels assureurs vous travaillez.",
        "pt": "Diga ao bot com quais seguradoras você tem contrato.",
        "zh": "告诉机器人您签约了哪些保险公司。",
        "ko": "봇에게 어떤 보험사와 계약되어 있는지 알려주세요.",
        "ja": "ボットに契約している保険会社を教えてください。",
        "vi": "Cho bot biết bạn đang hợp đồng với những hãng bảo hiểm nào.",
        "hi": "बॉट को बताएं कि आप किन बीमा कंपनियों से अनुबंधित हैं।",
        "tl": "Sabihin sa bot kung aling mga insurance carrier ang kasosyo mo.",
    },
}


def _t(key: str, lang: str = "en") -> str:
    """Get translated string. Falls back to English if key/lang missing."""
    entry = _STRINGS.get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get("en", key)


def detect_language(accept_language: str) -> str:
    """
    Parse the Accept-Language header and return the best supported language code.
    Example header: "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7"
    Returns: "es"
    """
    if not accept_language:
        return "en"

    # Parse header into (lang, quality) pairs
    langs = []
    for part in accept_language.split(","):
        part = part.strip()
        if not part:
            continue
        if ";q=" in part:
            code, q = part.split(";q=", 1)
            try:
                quality = float(q)
            except ValueError:
                quality = 0.0
        else:
            code = part
            quality = 1.0
        # Normalize: "es-MX" → "es"
        code = code.strip().split("-")[0].lower()
        langs.append((code, quality))

    # Sort by quality descending
    langs.sort(key=lambda x: x[1], reverse=True)

    # Return first supported match
    for code, _ in langs:
        if code in SUPPORTED_LANGUAGES:
            return code

    return "en"
