module.exports = {
  apps: [{
    name: "tg-monitor",
    script: "web_app.py",
    cwd: __dirname,
    interpreter: "python3",
    instances: 1,
    exec_mode: "fork",
    autorestart: true,
    watch: false,
    max_memory_restart: "500M",
    env: {
      PYTHONUNBUFFERED: "1",
    },
    error_file: "./logs/pm2-error.log",
    out_file: "./logs/pm2-out.log",
    log_date_format: "YYYY-MM-DD HH:mm:ss Z",
    time: true,
  }]
};