console.log("Dos Tiris Website Loaded");

const chatButton = document.getElementById("dostiris-chat-button");
const chatClose = document.getElementById("dostiris-chat-close");
const chatWidget = document.getElementById("dostiris-chat-widget");

if (chatButton && chatClose && chatWidget) {
    chatButton.style.visibility = "hidden";
    chatButton.style.opacity = "0";
    chatButton.style.transform = "translateY(8px) scale(0.96)";


    const launcherSvg = `
        <svg class="dt-mia-launcher-svg" viewBox="0 0 100 100" role="img" aria-hidden="true" focusable="false">
            <path
                d="M75.5 28.8C68.5 21.6 58.7 17.4 48.2 17.4C26.9 17.4 9.6 34.1 9.6 54.6c0 7.6 2.4 14.7 6.5 20.5L12.2 91l16.7-5.2c5.8 3.8 12.9 6 20.5 6c21.3 0 38.6-16.7 38.6-37.2c0-5.1-1.1-9.9-3.1-14.3"
                fill="none"
                stroke="var(--dt-widget-ring)"
                stroke-width="8.2"
                stroke-linecap="round"
                stroke-linejoin="round"
            />
            <path
                d="M41.6 35.2c3.4 0 5.4 2.1 8.4 2.1s5-2.1 8.4-2.1c7.1 0 12.1 5.9 11 14.1c-.9 6.3-3.1 11.1-4.2 18.4c-.8 5.4-2.8 11.3-7.1 11.3c-3.6 0-4.6-4.7-5.4-9.3c-.5-3.3-1.3-6.2-2.7-6.2s-2.2 2.9-2.7 6.2c-.8 4.6-1.8 9.3-5.4 9.3c-4.3 0-6.3-5.9-7.1-11.3c-1.1-7.3-3.3-12.1-4.2-18.4c-1.1-8.2 3.9-14.1 11-14.1Z"
                fill="var(--dt-widget-tooth)"
            />
            <path
                d="M42.4 39.6c3.2 1.9 6.1 2.8 9.1 2.8c2.8 0 5.8-.8 9.4-2.8"
                fill="none"
                stroke="rgba(6, 182, 212, 0.55)"
                stroke-width="2.2"
                stroke-linecap="round"
            />
            <path
                d="M76.8 20.9l2.7 7.1l7.2 2.7l-7.2 2.7l-2.7 7.2l-2.7-7.2l-7.2-2.7l7.2-2.7l2.7-7.1Z"
                fill="var(--dt-widget-sparkle)"
                opacity="0.96"
            />
            <path
                d="M64.4 48.2l1.7 4.3l4.3 1.7l-4.3 1.7l-1.7 4.3l-1.7-4.3l-4.3-1.7l4.3-1.7l1.7-4.3Z"
                fill="var(--dt-widget-sparkle)"
                opacity="0.86"
            />
        </svg>`;

    function installLauncherIcon() {
        chatButton.innerHTML = launcherSvg;
    }

    function getWidgetConfigUrl() {
        try {
            const frameUrl = new URL(chatWidget.getAttribute("src"), window.location.href);
            const clientKey = frameUrl.searchParams.get("client_key") || "demo_clinic_key";
            const configUrl = new URL("/chat/config", frameUrl.origin);
            configUrl.searchParams.set("client_key", clientKey);
            return configUrl.toString();
        } catch (error) {
            return null;
        }
    }

    function setCssVar(name, value) {
        if (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value.trim())) {
            chatButton.style.setProperty(name, value.trim());
        }
    }

    function revealLauncher() {
        window.requestAnimationFrame(() => {
            chatButton.style.transition = [
                "opacity 0.18s ease",
                "transform 0.2s ease",
                "box-shadow 0.2s ease",
                "filter 0.2s ease",
                "border-color 0.2s ease"
            ].join(", ");

            chatButton.style.visibility = "visible";
            chatButton.style.opacity = "1";
            chatButton.style.transform = "";
        });
    }

    async function applyLauncherTheme() {
        const configUrl = getWidgetConfigUrl();

        if (!configUrl || !window.fetch) {
            revealLauncher();
            return;
        }

        try {
            const response = await fetch(configUrl, { mode: "cors", credentials: "omit" });

            if (!response.ok) {
                revealLauncher();
                return;
            }

            const config = await response.json();
            const theme = config.launcher_theme || config.mia_launcher_theme || config.theme || {};

            setCssVar("--dt-widget-primary", theme.primary);
            setCssVar("--dt-widget-secondary", theme.secondary);
            setCssVar("--dt-widget-accent", theme.accent);
            setCssVar("--dt-widget-ring", theme.ring);
            setCssVar("--dt-widget-tooth", theme.primary || theme.tooth);
            setCssVar("--dt-widget-sparkle", theme.sparkle);
        } catch (error) {
            // Keep the default launcher colors if the public config request is unavailable.
        } finally {
            revealLauncher();
        }
    }

    installLauncherIcon();
    applyLauncherTheme();

    let widgetAnimationTimer = null;

    function clearWidgetAnimationTimer() {
        if (widgetAnimationTimer) {
            window.clearTimeout(widgetAnimationTimer);
            widgetAnimationTimer = null;
        }
    }

    function openChatWidget() {
        clearWidgetAnimationTimer();

        chatWidget.style.display = "block";
        chatClose.style.display = "block";

        chatButton.style.pointerEvents = "none";
        chatButton.style.opacity = "0";
        chatButton.style.transform = "translateY(8px) scale(0.9)";

        chatWidget.classList.remove("dt-widget-open");
        chatWidget.classList.remove("dt-widget-closing");

        // Force the browser to register the hidden starting state before animating open.
        void chatWidget.offsetHeight;

        window.requestAnimationFrame(() => {
            chatWidget.classList.add("dt-widget-open");
            chatClose.classList.add("dt-close-open");
        });

        widgetAnimationTimer = window.setTimeout(() => {
            chatButton.style.visibility = "hidden";
        }, 220);
    }

    function closeChatWidget() {
        clearWidgetAnimationTimer();

        chatWidget.classList.remove("dt-widget-open");
        chatWidget.classList.add("dt-widget-closing");
        chatClose.classList.remove("dt-close-open");

        chatButton.style.display = "grid";
        chatButton.style.visibility = "visible";

        window.requestAnimationFrame(() => {
            chatButton.style.pointerEvents = "";
            chatButton.style.opacity = "1";
            chatButton.style.transform = "";
        });

        widgetAnimationTimer = window.setTimeout(() => {
            chatWidget.style.display = "none";
            chatWidget.classList.remove("dt-widget-closing");
            chatClose.style.display = "none";
        }, 260);
    }

    chatButton.addEventListener("click", openChatWidget);
    chatClose.addEventListener("click", closeChatWidget);
}

const demoForm = document.getElementById("demoRequestForm");

console.log("Demo form found:", demoForm);

if (demoForm) {
    demoForm.addEventListener("submit", async function (e) {
        e.preventDefault();

        const formData = new FormData(demoForm);

        const payload = {
            name: formData.get("name"),
            practice_name: formData.get("practice_name"),
            email: formData.get("email"),
            phone: formData.get("phone"),
            website: formData.get("website"),
            interest: formData.get("interest"),
            message: formData.get("message")
        };

        try {
            const response = await fetch("https://ai-dental-chatbot.onrender.com/demo-request", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const errorData = await response.json().catch(() => null);
                throw new Error(errorData?.detail || "Request failed");
            }

            window.location.href = "thank-you.html";

        } catch (error) {
            alert(error.message || "Sorry, something went wrong. Please try again.");
        }
    });
}
