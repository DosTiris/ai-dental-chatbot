console.log("Dos Tiris Website Loaded");

const chatButton = document.getElementById("dostiris-chat-button");
const chatClose = document.getElementById("dostiris-chat-close");
const chatWidget = document.getElementById("dostiris-chat-widget");

if (chatButton && chatClose && chatWidget) {

    chatButton.addEventListener("click", () => {
        chatWidget.style.display = "block";
        chatClose.style.display = "block";
        chatButton.style.display = "none";
    });

    chatClose.addEventListener("click", () => {
        chatWidget.style.display = "none";
        chatClose.style.display = "none";
        chatButton.style.display = "block";
    });

}

const demoForm = document.getElementById("demoRequestForm");

console.log("Demo form found:", demoForm)

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
                throw new Error("Request failed");
            }

            window.location.href = "thank-you.html";

        } catch (error) {
            alert("Sorry, something went wrong. Please try again.");
        }
    });
}

 