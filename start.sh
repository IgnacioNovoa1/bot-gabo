#!/bin/bash

while true
do
    echo "--- $(date) --- Iniciando bot..."
    python main.py
    echo "--- $(date) --- Bot se ha detenido. Reiniciando en 5 segundos..."
    sleep 5
done